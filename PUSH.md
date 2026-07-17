# GitHubへの公開手順（katu09161004）

> このリポジトリは私（AI）からは直接pushできません（あなたのGitHub認証情報を扱わないため）。
> 以下をあなたの手元で実行してください。数コマンドで公開＋プレビュー公開まで完了します。

推奨リポジトリ名：**`nursing-worktime`**（README/プレビュー内のURLがこの名前前提）

---

## 方法A：gh CLI がある場合（いちばん速い）

このフォルダ内で：

```bash
gh auth status                      # 未ログインなら gh auth login
gh repo create nursing-worktime --public --source=. --remote=origin --push \
  --description "看護業務量調査ツール（AI効果測定ベースライン・多職種協働加算エビデンス用）"
```

これで public リポジトリ作成＋push まで完了。続けてGitHub Pagesを有効化：

```bash
gh api -X POST repos/katu09161004/nursing-worktime/pages \
  -f "source[branch]=main" -f "source[path]=/docs"
```

---

## 方法B：Webでリポジトリを作ってから push

1. GitHubで空の public リポジトリ `nursing-worktime` を作成（README等は追加しない）。
2. このフォルダで：

```bash
git init                            # 既に初期化済みならスキップ
git add -A
git commit -m "看護業務量調査ツール（AI効果測定ベースライン用）"
git branch -M main
git remote add origin https://github.com/katu09161004/nursing-worktime.git
git push -u origin main
```

3. GitHubの **Settings → Pages** で
   **Source = Deploy from a branch**、**Branch = main / (docs)** を選んで Save。

---

## 公開後のURL

- リポジトリ：`https://github.com/katu09161004/nursing-worktime`
- **プレビュー（岡田先生に共有）：`https://katu09161004.github.io/nursing-worktime/`**
  （Pages有効化から反映まで1〜2分）

プレビューでは、記録画面のデモ（実際にボタンが押せる）と集計ダッシュボードのデモが
ブラウザ内だけで動きます（サーバ不要・データ保存なし）。

---

## 公開前の最終確認（済み）

- ✅ 施設名・実病棟名・実氏名は含めていない（病棟A/B/C・N-01〜の汎用値）
- ✅ `worktime.db`（記録データ）は `.gitignore` で除外＝コミットされない
- ✅ 施設固有値は `config_local.py`（`.gitignore`対象）に置く運用
- ⚠ `LICENSE` に著者名（Katsuyoshi Fujita）を記載。ハンドル名のみに変えたい場合は編集を。

## 実運用サーバへの反映（DL380）

公開リポジトリは汎用値。実際の病棟名・スタッフIDは、DL380側に置く `config_local.py`
（gitに上げない）で上書きする。区分は `main.py` の `CATEGORIES` を編集。
