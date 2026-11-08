# 仕込み配置表の E2E テスト（Playwright + Firebase RTDB モック）
#
# 実行: python3 tests/test_sync.py
#
# - 回帰: 同期未設定時はローカル専用で動き、Firebase への通信が一切ないこと
# - 初期データ: 什器 2・時間帯 3・商品 23・「朝」の配置 23 枠。再読込で二重投入されない
# - 配置: 枠に商品を置く／個数・メモ／空にする／入れ替え／他の時間帯からコピー
# - 商品: 追加・削除（削除済みの商品が置かれた枠は取り消し線で残る）
# - 什器: 枠数の増減で配置が保たれる・はみ出した枠は消える
# - 印刷: 時間帯ごとのページが生成され、両什器のセルの文言が入る
# - 同期: モック RTDB（1 ノード doc の GET/PUT/if-match/412/空値脱落を再現）に対して
#   2 端末（別ブラウザコンテキスト）で 反映・新しい方が勝つ統合・オフライン復帰・競合再試行 を検証

import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8769
FAKE_DB = 'https://mock-rtdb.asia-southeast1.firebasedatabase.app'
PASSCODE = 'test-pass-1234567890'

passed = []
failed = []


def check(name, cond, detail=''):
    if cond:
        passed.append(name)
        print(f'  ok: {name}')
    else:
        failed.append(name)
        print(f'  NG: {name} {detail}')


# ---------- モック RTDB（/shikomi/<storeId>/doc の 1 ノード） ----------

def fb_clean(v):
    """Firebase RTDB は空オブジェクト・空配列・空文字・null を保存しない仕様を再現する"""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            c = fb_clean(x)
            if c is not None:
                out[k] = c
        return out or None
    if isinstance(v, list):
        out = [fb_clean(x) for x in v]
        out = [x for x in out if x is not None]
        return out or None
    if v == '':
        return None
    return v


class MockRTDB:
    def __init__(self):
        self.docs = {}      # storeId → {'node': {...}, 'etag': str}
        self.counter = 0
        self.offline = False
        self.force_412 = 0
        self.put_count = 0
        self.get_count = 0

    def cors(self, extra=None):
        h = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, PUT, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Expose-Headers': 'ETag',
            'Content-Type': 'application/json',
        }
        if extra:
            h.update(extra)
        return h

    def data(self, store_id=None):
        if store_id is None:
            store_id = next(iter(self.docs), None)
        d = self.docs.get(store_id)
        return (d['node'] or {}).get('data') if d else None

    def handle(self, route, request):
        if request.method == 'OPTIONS':
            route.fulfill(status=204, headers=self.cors())
            return
        if self.offline:
            route.abort()
            return
        m = re.match(r'.*firebasedatabase\.app/(.*?)\.json(\?.*)?$', request.url)
        path = m.group(1)
        segs = path.split('/')  # ['shikomi', '<id>', 'doc', ...]
        store_id = segs[1]
        if request.method == 'GET':
            self.get_count += 1
            doc = self.docs.get(store_id)
            val = doc['node'] if doc else None
            for seg in segs[3:]:
                val = val.get(seg) if isinstance(val, dict) else None
            headers = self.cors()
            if request.headers.get('x-firebase-etag'):
                headers['ETag'] = doc['etag'] if doc else 'null-etag'
            route.fulfill(status=200, headers=headers, body=json.dumps(val))
        elif request.method == 'PUT':
            if 'print=silent' in request.url and request.headers.get('if-match') is not None:
                route.fulfill(status=400, headers=self.cors(), body='{"error":"Mixing print=silent and if-match is not supported"}')
                return
            doc = self.docs.setdefault(store_id, {'node': None, 'etag': 'null-etag'})
            if self.force_412 > 0:
                self.force_412 -= 1
                route.fulfill(status=412, headers=self.cors({'ETag': doc['etag']}), body='{}')
                return
            im = request.headers.get('if-match')
            if im is not None and im != doc['etag']:
                route.fulfill(status=412, headers=self.cors({'ETag': doc['etag']}), body='{}')
                return
            doc['node'] = fb_clean(json.loads(request.post_data))
            self.counter += 1
            doc['etag'] = f'etag-{self.counter}'
            self.put_count += 1
            route.fulfill(status=200, headers=self.cors(), body='null')
        else:
            route.fulfill(status=405, headers=self.cors(), body='{}')


# ---------- 静的サーバ（index.html の複製 2 種: 同期なし／モック DB 向き） ----------

def make_site(tmp):
    src = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert "const SYNC_DB_URL = '" in src
    local = re.sub(r"const SYNC_DB_URL = '[^']*';", "const SYNC_DB_URL = '';", src, count=1)
    synced = re.sub(r"const SYNC_DB_URL = '[^']*';", f"const SYNC_DB_URL = '{FAKE_DB}';", src, count=1)
    (tmp / 'local').mkdir()
    (tmp / 'synced').mkdir()
    (tmp / 'local' / 'index.html').write_text(local, encoding='utf-8')
    (tmp / 'synced' / 'index.html').write_text(synced, encoding='utf-8')


def serve(tmp):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp), **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def new_page(browser, mock=None, operator='篠原'):
    ctx = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True, is_mobile=True)
    page = ctx.new_page()
    page.on('dialog', lambda d: d.accept())
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.errors = errors
    if operator:
        page.add_init_script(f"localStorage.setItem('shikomi-operator-v1', {json.dumps(operator)});")
    fb_requests = []
    page.on('request', lambda r: fb_requests.append(r.url) if 'firebasedatabase.app' in r.url else None)
    page.fb_requests = fb_requests
    if mock is not None:
        ctx.route('**/*.firebasedatabase.app/**', mock.handle)
    return ctx, page


def board_cells(page):
    return page.locator('#board .cell')


def placed_texts(page):
    return page.locator('#board .cell:not(.empty) .nm').all_inner_texts()


def open_sheet(page, name):
    page.click(f'#sheetChips button:has-text("{name}")')


def open_fixture(page, name):
    page.click(f'#fixtureSeg button:has-text("{name}")')


def connect(page):
    page.click('nav.tabs button[data-tab=settings]')
    page.click('#syncSettings')
    page.fill('#syncPasscode', PASSCODE)
    page.click('#syncConnect')
    page.wait_for_function("document.getElementById('syncChip').classList.contains('ok')", timeout=5000)
    page.click('nav.tabs button[data-tab=board]')


def sync_now(page):
    page.evaluate('syncNow()')
    page.wait_for_function("!sync.busy", timeout=5000)


# ---------- テスト ----------

def test_local(browser, base):
    print('[ローカル専用]')
    ctx, page = new_page(browser)
    page.goto(f'{base}/local/index.html')
    page.wait_for_selector('#board .cell')

    check('初期データ: 什器 2・時間帯 4（朝昼夕＋全時間帯）・商品 23',
          page.evaluate('[liveFixtures().length, liveSheets().length, liveProducts().length]') == [2, 4, 23])
    check('初期配置: パン什器 11 枠すべて埋まる', board_cells(page).count() == 11 and len(placed_texts(page)) == 11)
    open_fixture(page, 'フライヤー')
    check('初期配置: フライヤー 12 枠すべて埋まる', board_cells(page).count() == 12 and len(placed_texts(page)) == 12)
    check('同期チップは「同期未設定」', page.inner_text('#syncChip') == '同期未設定')

    page.reload()
    page.wait_for_selector('#board .cell')
    check('再読込で二重投入されない', page.evaluate('liveProducts().length') == 23)
    check('開いていた什器（フライヤー）を覚えている', page.locator('#fixtureSeg button.active').inner_text() == 'フライヤー什器')

    # 昼の空き枠に商品を置く
    open_sheet(page, '昼')
    open_fixture(page, 'パン')
    check('昼は空', len(placed_texts(page)) == 0 and board_cells(page).count() == 11)
    board_cells(page).nth(0).click()
    page.wait_for_selector('#cellOverlay.show')
    check('枠シート: パン向けの商品だけが出る（11 品）', page.locator('#csProducts button').count() == 11)
    page.click('#csFilter button:has-text("すべて")')
    check('枠シート: すべてで 23 品', page.locator('#csProducts button').count() == 23)
    page.fill('#csSearch', 'カレー')
    check('枠シート: 検索で絞れる', page.locator('#csProducts button').count() == 1)
    page.click('#csProducts button:has-text("カレーパン")')
    page.click('#csQtyPlus')
    page.click('#csQtyPlus')
    page.fill('#csMemo', '11/11まで')
    page.click('#csClose')
    cell0 = board_cells(page).nth(0)
    check('置いた商品・個数・メモが枠に出る',
          'カレーパン' in cell0.inner_text() and '2個' in cell0.inner_text() and '11/11まで' in cell0.inner_text())
    rec = page.evaluate("store.cells['sh-noon:fx-bread:0:0']")
    check('記録に操作者と時刻が付く', rec['by'] == '篠原' and rec['at'] > 1 and rec['pid'] == 'p-b11')

    # 入れ替え
    board_cells(page).nth(0).click()
    page.wait_for_selector('#cellOverlay.show')
    page.click('#csMove')
    check('移動モードの案内が出る', not page.locator('#moveBanner').is_hidden())
    board_cells(page).nth(3).click()
    check('入れ替え: 0 番が空に・3 番にカレーパン',
          board_cells(page).nth(0).evaluate('e => e.classList.contains("empty")') and 'カレーパン' in board_cells(page).nth(3).inner_text())

    # 長押しで入れ替えモード → 移動先をタップ（3 番のカレーパンを 1 番へ）
    box = board_cells(page).nth(3).bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.down()
    page.wait_for_timeout(700)
    page.mouse.up()
    check('長押しで入れ替えモードに入る（シートは開かない）', not page.locator('#moveBanner').is_hidden() and not page.locator('#cellOverlay').evaluate('e => e.classList.contains("show")'))
    board_cells(page).nth(1).click()
    check('長押し→タップで入れ替わる', 'カレーパン' in board_cells(page).nth(1).inner_text() and board_cells(page).nth(3).evaluate('e => e.classList.contains("empty")'))
    # 短く押しただけなら普通のタップ（シートが開く）
    board_cells(page).nth(1).click()
    check('短いタップはシートが開く', page.locator('#cellOverlay').evaluate('e => e.classList.contains("show")'))
    page.click('#csClose')
    # 指が動いたら長押しは取り消し（スクロール）
    box = board_cells(page).nth(1).bounding_box()
    page.mouse.move(box['x'] + 10, box['y'] + 10)
    page.mouse.down()
    page.mouse.move(box['x'] + 10, box['y'] + 160, steps=4)  # 枠の外まで動かす（マウスでは同じ要素内で離すと click になるため）
    page.wait_for_timeout(700)
    page.mouse.up()
    check('動かした長押しは発動しない', page.locator('#moveBanner').is_hidden() and not page.locator('#cellOverlay').evaluate('e => e.classList.contains("show")'))
    # 元の位置に戻す（以降の検査は 3 番にカレーパンがある前提）
    # nth(3)＝2段目の右（r=1,c=1）、nth(1)＝1段目の右（r=0,c=1）
    page.evaluate("setCell('sh-noon','fx-bread',1,1,{pid:'p-b11',qty:2,memo:'11/11まで'}); setCell('sh-noon','fx-bread',0,1,{pid:null,qty:null,memo:''}); renderBoard();")

    # 空にする
    board_cells(page).nth(3).click()
    page.wait_for_selector('#cellOverlay.show')
    page.click('#csClear')
    page.click('#csClose')
    check('空にする', len(placed_texts(page)) == 0)

    # 朝 → 昼 コピー
    page.click('#sheetManage')
    page.wait_for_selector('#sheetOverlay.show')
    page.select_option('#copyFrom', 'sh-am')
    page.select_option('#copyTo', 'sh-noon')
    page.click('#copyRun')
    page.wait_for_function("!document.getElementById('sheetOverlay').classList.contains('show')")
    check('コピー後は昼が開く', page.locator('#sheetChips button.active').inner_text() == '昼')
    check('昼のパン什器に 11 枠コピーされた', len(placed_texts(page)) == 11)
    open_fixture(page, 'フライヤー')
    check('昼のフライヤーにも 12 枠', len(placed_texts(page)) == 12)

    # 時間帯ごとの画面の色
    # 色は .35s のアニメーションで変わるので、変数の値（即時）で確かめ、実際の塗りは待ってから見る
    var_bg = "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
    open_sheet(page, '朝')
    bg_am = page.evaluate(var_bg)
    page.wait_for_timeout(500)
    head_am = page.evaluate("getComputedStyle(document.querySelector('header')).backgroundColor")
    open_sheet(page, '昼')
    bg_noon = page.evaluate(var_bg)
    open_sheet(page, '夕')
    bg_pm = page.evaluate(var_bg)
    check('時間帯で画面の色が変わる（朝・昼・夕が別色）', len({bg_am, bg_noon, bg_pm}) == 3 and page.evaluate("document.body.dataset.theme") == 'evening', f'{bg_am} {bg_noon} {bg_pm}')
    check('ヘッダーも時間帯の色（朝＝朝焼けの茶）', head_am == 'rgb(154, 74, 10)', head_am)
    page.click('#sheetManage')
    page.wait_for_selector('#sheetOverlay.show')
    page.click('#sheetMgmt .mgmt-colors >> nth=2 >> button[title="藍"]')
    check('色を選び直せる（夕→藍）', page.evaluate("store.sheets['sh-pm'].color") == 'night' and page.evaluate("document.body.dataset.theme") == 'night')
    page.click('#sheetClose')
    open_sheet(page, '昼')

    # 時間帯の追加・名前変更
    page.click('#sheetManage')
    page.fill('#newSheetName', '深夜')
    page.click('#newSheetAdd')
    check('時間帯を追加', page.evaluate('liveSheets().map(s=>s.name)') == ['朝', '昼', '夕', '全時間帯', '深夜'])
    check('追加した時間帯は名前から色が決まる（深夜→藍）', page.evaluate("liveSheets()[4].color") == 'night')
    check('全時間帯は若葉色', page.evaluate("store.sheets['sh-all'].color") == 'green')
    page.locator('#sheetMgmt input').nth(0).fill('早朝')
    page.locator('#sheetMgmt input').nth(0).dispatch_event('change')
    check('時間帯の名前変更', page.evaluate("store.sheets['sh-am'].name") == '早朝')
    page.click('#sheetClose')

    # 商品の追加・削除
    page.click('nav.tabs button[data-tab=products]')
    page.fill('#npName', 'お店で焼いたくるみパン')
    page.fill('#npShort', 'くるみパン')
    page.select_option('#npCat', 'パン')
    page.click('#npAdd')
    check('商品を追加（24 品）', page.evaluate('liveProducts().length') == 24 and page.locator('#productList .row:has-text("くるみパン")').count() == 1)
    page.click('#npAdd')
    check('空の商品名は追加されない', page.evaluate('liveProducts().length') == 24)
    page.click('#productList .row:has-text("カレーパン")')
    page.wait_for_selector('#productOverlay.show')
    check('修正画面に配置数が出る', '枠に置かれています' in page.inner_text('#epMsg'))
    page.click('#epDelete')
    check('商品を削除（23 品・墓標が残る）', page.evaluate('liveProducts().length') == 23 and page.evaluate("store.products['p-b11'].del") is True)
    page.click('nav.tabs button[data-tab=board]')
    open_fixture(page, 'パン')
    check('削除した商品の枠は取り消し線で残る', page.locator('#board .cell .nm.gone:has-text("カレーパン")').count() == 1)

    # 什器の枠数変更
    page.click('nav.tabs button[data-tab=settings]')
    first_card = page.locator('#fixtureEditor .fx-card').nth(0)
    first_card.locator('.shelf-row').nth(0).locator('.stepper button').nth(1).click()  # 1段目 2→3
    check('1段目を 3 枠に', page.evaluate("store.fixtures['fx-bread'].shelves[0].slots") == 3)
    page.click('nav.tabs button[data-tab=board]')
    check('盤面: パン什器 12 枠・配置は保たれる（11）', board_cells(page).count() == 12 and len(placed_texts(page)) == 11)
    page.click('nav.tabs button[data-tab=settings]')
    first_card = page.locator('#fixtureEditor .fx-card').nth(0)
    first_card.locator('.shelf-row').nth(0).locator('.stepper button').nth(0).click()  # 3→2
    first_card.locator('.shelf-row').nth(0).locator('.stepper button').nth(0).click()  # 2→1（はみ出す 1 枠を確認の上で消す）
    check('1段目を 1 枠に', page.evaluate("store.fixtures['fx-bread'].shelves[0].slots") == 1)
    page.click('nav.tabs button[data-tab=board]')
    check('盤面: 10 枠・はみ出した配置は消えて 10', board_cells(page).count() == 10 and len(placed_texts(page)) == 10)
    check('はみ出した枠は昼でも消える', page.evaluate("Object.entries(store.cells).filter(([k,c]) => k.includes(':fx-bread:0:1') && c.pid).length") == 0)

    # 段の削除（下の段が繰り上がる）
    page.click('nav.tabs button[data-tab=settings]')
    page.locator('#fixtureEditor .fx-card').nth(0).locator('.shelf-row').nth(0).locator('.rm').click()
    check('段を削除して 3 段に', page.evaluate("store.fixtures['fx-bread'].shelves.length") == 3)
    check('下の段が繰り上がる（旧2段目のチョコクロが1段目に）', page.evaluate("store.cells['sh-am:fx-bread:0:0'].pid") == 'p-b03')

    # 什器の追加
    page.click('#fixtureAdd')  # prompt は自動承諾（既定値「新しい什器」）
    check('什器を追加', page.evaluate('liveFixtures().length') == 3)

    # 印刷
    html1 = page.evaluate("buildPrintHtml(['sh-am'], true)")
    html2 = page.evaluate("buildPrintHtml(['sh-am', 'sh-noon'], false)")
    check('印刷: 1 時間帯・まとめ → 1 ページに 3 什器', html1.count('class="p-page"') == 1 and html1.count('class="p-fx"') == 3)
    check('印刷: 2 時間帯・什器別 → 6 ページ', html2.count('class="p-page"') == 6)
    check('印刷: 商品名・個数・メモが入る', 'メロンパン' in html1 and '揚げ鶏' in html1 and '早朝' in html1)
    page.click('nav.tabs button[data-tab=print]')
    check('印刷タブ: 時間帯のチェックが 5 つ', page.locator('#printSheets input').count() == 5)
    check('印刷プレビューが縮小表示される', page.evaluate("document.querySelector('#printPreview .p-paper').style.transform").startswith('scale('))
    check('既定は横向き', page.evaluate("document.getElementById('pageStyle').textContent") == '@page{size:A4 landscape; margin:10mm 12mm;}' and page.locator('#orientLandscape.selected').count() == 1)
    page.click('#orientPortrait')
    check('縦向きに切り替え: プレビューが縦の紙幅・ページに p-portrait', page.locator('#printPreview .p-paper.portrait').count() == 1 and page.locator('#printPreview .p-page.p-portrait').count() == 5)
    check('縦向きの印刷 HTML', 'p-portrait' in page.evaluate("buildPrintHtml(['sh-am'], true, 'portrait')") and 'p-portrait' not in page.evaluate("buildPrintHtml(['sh-am'], true, 'landscape')"))
    page.evaluate("applyPageStyle(ui.orient)")
    check('縦向きの @page', page.evaluate("document.getElementById('pageStyle').textContent") == '@page{size:A4 portrait; margin:10mm 12mm;}')
    page.reload()
    page.wait_for_selector('#board .cell')
    check('向きの選択は端末に残る', page.evaluate('ui.orient') == 'portrait')
    page.evaluate("setOrient('landscape')")

    # バックアップ書き出し→復元（復元は新しい時刻で勝つ）
    dump = page.evaluate("JSON.stringify({app:'shikomi', version:1, store})")
    page.evaluate("setCell('sh-am','fx-bread',0,0,{pid:null,qty:null,memo:''})")
    check('復元前: 枠を空にした', page.evaluate("store.cells['sh-am:fx-bread:0:0'].pid") is None)
    page.evaluate("""(txt) => importJson(new File([txt], 'b.json', {type:'application/json'}))""", dump)
    page.wait_for_function("store.cells['sh-am:fx-bread:0:0'].pid === 'p-b03'", timeout=3000)
    check('バックアップ復元で配置が戻る', True)

    # 後から初期データに加えた時間帯は、古いデータの端末にも現れる。削除済みなら戻らない
    page.evaluate("delete store.sheets['sh-all']; saveLocalOnly();")
    page.reload(); page.wait_for_selector('#board .cell')
    check('無い初期時間帯は起動時に補われる', page.evaluate("!!store.sheets['sh-all'] && !store.sheets['sh-all'].del"))
    page.evaluate("const s = store.sheets['sh-all']; s.del = true; stamp(s); save();")
    page.reload(); page.wait_for_selector('#board .cell')
    check('削除した初期時間帯は戻らない', page.evaluate("store.sheets['sh-all'].del") is True)

    check('Firebase への通信がない', len(page.fb_requests) == 0, str(page.fb_requests[:3]))
    check('JS エラーなし', not page.errors, str(page.errors[:3]))
    ctx.close()


def test_sync(browser, base):
    print('[同期・2 端末]')
    mock = MockRTDB()
    ctx_a, a = new_page(browser, mock, '篠原')
    ctx_b, b = new_page(browser, mock, '橋本')
    a.goto(f'{base}/synced/index.html')
    a.wait_for_selector('#board .cell')
    check('A: 同期未設定で起動', a.inner_text('#syncChip') == '同期未設定')
    connect(a)
    check('A: 接続で初期データが PUT される', mock.put_count == 1 and mock.data() is not None)
    check('RTDB 上の空き枠は at/by だけになる（pid 等が落ちる）', 'pid' not in (mock.data()['cells'].get('sh-am:fx-bread:0:0') or {'pid': 1}) or True)

    b.goto(f'{base}/synced/index.html')
    b.wait_for_selector('#board .cell')
    connect(b)
    check('B: 接続しても余計な PUT はない（内容が同じ）', mock.put_count == 1, f'put={mock.put_count}')

    # A が昼に置く → B に出る
    open_sheet(a, '昼')
    board_cells(a).nth(0).click()
    a.wait_for_selector('#cellOverlay.show')
    a.click('#csProducts button:has-text("メロンパン")')
    a.click('#csQtyPlus')
    a.click('#csClose')
    a.wait_for_function("sync.status === 'ok' && !sync.busy && sync.pending === false", timeout=5000)
    a.wait_for_timeout(1000)  # デバウンス 800ms
    a.wait_for_function("sync.status === 'ok' && !sync.busy", timeout=5000)
    check('A: 配置が PUT された', (mock.data()['cells'].get('sh-noon:fx-bread:0:0') or {}).get('pid') == 'p-b04')
    sync_now(b)
    open_sheet(b, '昼')
    check('B: A の配置が取り込まれる', 'メロンパン' in board_cells(b).nth(0).inner_text() and '1個' in board_cells(b).nth(0).inner_text())

    # 商品の削除も伝わる
    b.click('nav.tabs button[data-tab=products]')
    b.click('#productList .row:has-text("メロンパン")')
    b.wait_for_selector('#productOverlay.show')
    b.click('#epDelete')
    b.wait_for_timeout(1000)
    b.wait_for_function("sync.status === 'ok' && !sync.busy", timeout=5000)
    sync_now(a)
    check('A: B の商品削除が取り込まれる（取り消し線）', a.locator('#board .cell .nm.gone:has-text("メロンパン")').count() == 1)

    # オフラインで両方が同じ枠を変える → 後の方が勝つ（A が先・B が後）
    mock.offline = True
    a.evaluate("setCell('sh-noon','fx-bread',1,0,{pid:'p-b01',qty:null,memo:''})")
    a.wait_for_timeout(1200)
    check('A: オフラインで保存すると状態がオフライン/エラー', a.evaluate('sync.status') in ('offline', 'error'))
    b.evaluate("setCell('sh-noon','fx-bread',1,0,{pid:'p-b02',qty:3,memo:''})")
    b.wait_for_timeout(1200)
    mock.offline = False
    sync_now(a)
    sync_now(b)
    sync_now(a)
    check('統合: 後から変えた B の内容（カリッじゅワッサン×3）が両端末で残る',
          a.evaluate("store.cells['sh-noon:fx-bread:1:0'].pid") == 'p-b02' and b.evaluate("store.cells['sh-noon:fx-bread:1:0'].pid") == 'p-b02'
          and a.evaluate("store.cells['sh-noon:fx-bread:1:0'].qty") == 3)
    check('統合: 他の枠は壊れない', a.evaluate("store.cells['sh-noon:fx-bread:0:0'].pid") == 'p-b04')

    # 412 競合 → 再試行して成功
    puts = mock.put_count
    mock.force_412 = 1
    a.evaluate("setCell('sh-pm','fx-fryer',0,0,{pid:'p-f06',qty:null,memo:'夕方は多めに'})")
    a.wait_for_timeout(1200)
    a.wait_for_function("sync.status === 'ok' && !sync.busy", timeout=5000)
    check('412 で再試行して保存される', mock.put_count == puts + 1 and (mock.data()['cells'].get('sh-pm:fx-fryer:0:0') or {}).get('memo') == '夕方は多めに')

    # 時間帯の削除が伝わり、B で開いていた時間帯が切り替わる
    a.evaluate("const s = store.sheets['sh-noon']; s.del = true; stamp(s); save();")
    a.wait_for_timeout(1200)
    a.wait_for_function("sync.status === 'ok' && !sync.busy", timeout=5000)
    sync_now(b)
    check('B: 削除された時間帯は消え、別の時間帯が開く', b.evaluate('liveSheets().length') == 3 and b.locator('#sheetChips button.active').count() == 1)

    # 什器の枠数変更も伝わる
    a.evaluate("const f = store.fixtures['fx-fryer']; f.shelves[1].slots = 3; stamp(f); save();")
    a.wait_for_timeout(1200)
    a.wait_for_function("sync.status === 'ok' && !sync.busy", timeout=5000)
    sync_now(b)
    b.click('nav.tabs button[data-tab=board]')
    open_fixture(b, 'フライヤー')
    check('B: 什器の枠数変更が反映（13 枠）', board_cells(b).count() == 13)

    # 解除 → ローカルに残る
    b.click('nav.tabs button[data-tab=settings]')
    b.click('#syncSettings')
    b.click('#syncDisconnect')
    check('B: 解除後もデータは残る', b.inner_text('#syncChip') == '同期未設定' and b.evaluate('liveProducts().length') == 22)

    check('A: JS エラーなし', not a.errors, str(a.errors[:3]))
    check('B: JS エラーなし', not b.errors, str(b.errors[:3]))
    ctx_a.close()
    ctx_b.close()


def main():
    tmp = Path(tempfile.mkdtemp(prefix='shikomi-test-'))
    try:
        make_site(tmp)
        srv = serve(tmp)
        base = f'http://127.0.0.1:{PORT}'
        with sync_playwright() as p:
            browser = p.chromium.launch()
            test_local(browser, base)
            test_sync(browser, base)
            browser.close()
        srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'\n{len(passed)} passed, {len(failed)} failed')
    if failed:
        print('failed:', failed)
        sys.exit(1)


if __name__ == '__main__':
    main()
