# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

コンビニ店舗の**店内調理の什器（フライヤー・ベーカリー）に、どの段のどこに何を置くか**を携帯で管理するアプリ。
手書きの配置メモをやめ、スタッフ全員が同じ配置表を見て、A4 横で印刷もできる。

- `index.html` 1ファイルのアプリ（GitHub Pages で公開。現金管理アプリ・仕入台帳と同じ作り。仕入台帳 `~/work/仕入台帳/` から同期・印刷の仕組みを移植）
- `tests/test_sync.py` … Playwright E2E（モック RTDB・2 端末）
- `database.rules.json` / `.firebaserc` … Firebase プロジェクト `shikomi-sync` のルール
- `SETUP.md` … 管理者向けの初回設定手順

タブは「配置／商品／印刷／設定」。ヘッダーに開いている時間帯・最終変更、同期チップ、操作者チップ。

## アーキテクチャ

### 概念
- **什器（fixture）**: 段の並び。各段に枠数 1〜4（`shelves:[{slots:n}]`）。種類 `cat`（パン／フライヤー／その他）を持ち、枠をタップしたとき同じ種類の商品を先に出す
- **時間帯（sheet）**: 配置表の束（朝・昼・夕など）。追加・名前変更・並び替え・削除・他の時間帯からのコピー
- **商品（product）**: `name`（正式名）と `short`（枠に出す短い表示名）と `cat`
- **枠（cell）**: キー `"<sheetId>:<fixtureId>:<段>:<枠>"`（0 始まり）。値 `{pid, qty, memo, at, by}`。空き枠は `pid:null`

初期データ `SEED`（2026-11-08 の什器写真から）: パン什器 2/2/3/4・フライヤー什器 4/2/2/4・時間帯 朝昼夕・商品 23 品・「朝」に写真どおりの配置。
初期データの更新時刻は `SEED_AT = 1` で、どの端末の編集（`Date.now()`）よりも古いので、統合で必ず編集が勝つ。ID は固定（`fx-bread` / `sh-am` / `p-b01` …）なので複数端末がそれぞれ初期投入しても衝突しない。

### データモデル（状態スナップショット型・項目ごとに新しい方が勝つ）
```
store = { version, fixtures:{id→…}, products:{id→…}, sheets:{id→…}, cells:{key→…} }
```
- どの項目も `at`（更新時刻）と `by`（操作者）を持つ。変更は `stamp(obj)` で打つ
- 削除は墓標（`del:true`）で残す。削除済みの商品が置かれた枠は取り消し線で表示し、タップで差し替える
- `mergeStore(local, remote)`: 4 つの表それぞれでキーごとに `at` の大きい方（同時刻なら remote）。冪等・順序非依存
- `normalizeStore` は RTDB が落とす空値（空オブジェクト・空文字・null）を補完する。比較は `storeSig`（キー順を揃えた JSON）
- 履歴は持たない（仕入台帳のイベント追記型とは違う）。「昨日に戻したい」は JSON バックアップの復元で対応（復元側の `at` を今の時刻に打ち直して必ず勝たせる）
- localStorage キーは `shikomi-` 接頭辞（`shikomi-v1` / `shikomi-operator-v1` / `shikomi-sync-v1` / `shikomi-ui-v1`）

### 什器の編集の副作用
- 枠数を減らす・段を削除するとき、はみ出す枠に商品があれば confirm → `clearCells` で全時間帯ぶん空にする
- 段の削除は下の段の配置を 1 段ずつ繰り上げる（`removeShelf`）

### クラウド同期（Firebase Realtime Database / REST のみ）
仕入台帳の `syncMonth` を 1 ノード用に単純化したもの。
- パス `/shikomi/<storeId>/doc`（`{meta:{rev,at}, data: store}`）。`storeId = SHA-256('shikomi-store:' + パスコード)`
- `syncDoc`: GET（ETag 付き）→ `mergeStore` → 違えば if-match PUT → 412 なら再取得して 3 回まで再試行。800ms デバウンス、60 秒ごとに `meta` だけポーリング、`online`・`visibilitychange` で同期
- **別の Firebase プロジェクト `shikomi-sync`**（RTDB のルールはプロジェクトに 1 つで、デプロイで丸ごと上書きされるため、現金管理・予約台帳・仕入台帳とは分ける）。Spark 無料枠で足りる

### 印刷
- 画面には出さない `#printArea` に `buildPrintHtml(sheetIds, combine)` を流し込んで `window.print()`。`@page{size:A4 landscape}`
- 1 時間帯＝1 ページ。`combine` で全什器を左右に並べる（`.p-fixtures` は flex-wrap。iPhone の Safari が A4 縦で刷っても上下に積まれて崩れない）
- 画面のプレビューは同じ HTML を幅 273mm で組み、`transform: scale()` で縮小

## 技術方針（ユーザーと合意済みの制約）

- 単一の `index.html` に HTML / CSS / JS をすべてインラインで書く。ビルドツール・外部依存・CDN は使わない（Firebase も SDK を使わず REST + fetch のみ）
- ドラッグ＆ドロップは使わない。入れ替えは「枠をタップ → 別の枠と入れ替え → 移動先をタップ」
- UI は日本語。操作者は `OPERATORS` の固定名簿

## 実行方法

```bash
open index.html                      # ブラウザで開くだけ（同期は SYNC_DB_URL が空なら無効）
python3 tests/test_sync.py           # E2E（Playwright。モック RTDB。2 端末）
firebase deploy --only database      # ルールの反映（SETUP.md 参照。Firebase コンソールに貼ってもよい）
```
