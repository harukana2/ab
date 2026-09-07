# US Stock Scanner → Gmail

S&P 500 + Nasdaq 100 を対象に、出来高・値幅・トレンド・アナリスト評価などから
「デイトレード向け候補」と「長期・成長期待候補」を抽出し、1〜100の
利益期待スコア/リスクスコアと現在値・決算日をまとめてGmailに送信する
GitHub Actionsワークフローです。

## できないこと・注意点(必ず読んでください)

- **将来の利益や値動きを予測するものではありません。** スコアは出来高・
  ボラティリティ・トレンド・アナリスト目標株価など公開データから算出した
  相対的な目安であり、統計的な「利益が出る確率」ではありません。
- データソースは [yfinance](https://github.com/ranaroussi/yfinance)
  (Yahoo Financeの非公式ライブラリ)です。Webullなど証券会社のリアルタイム
  データとは異なり、遅延・欠損・不正確な値が含まれることがあります。
  **発注前に必ずブローカー側で最新の価格・決算日を確認してください。**
- 決算日は取得できないことがあります(`不明`と表示されます)。
- 本ツール・本READMEは投資助言ではありません。作者は金融アドバイザーでは
  ありません。投資判断は自己責任で行ってください。

## セットアップ

### 1. Gmailのアプリパスワードを発行する

1. Googleアカウントで **2段階認証を有効化**(未設定の場合)
2. https://myaccount.google.com/apppasswords にアクセス
3. アプリ名を適当に入力し、16桁のアプリパスワードを発行(スペースは除いてコピー)

通常のGmailパスワードはSMTP送信に使えないため、必ずアプリパスワードを使ってください。

### 2. このリポジトリをGitHubに作成し、Secretsを登録する

`Settings → Secrets and variables → Actions → New repository secret` から以下を登録:

| Secret名 | 内容 |
|---|---|
| `GMAIL_USER` | 送信元Gmailアドレス(例: `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | 手順1で発行した16桁のアプリパスワード |
| `GMAIL_TO` | 受信先メールアドレス(自分宛でもOK) |

### 3. スケジュールの確認・調整

`.github/workflows/scan.yml` の `cron` は **UTC** で指定します。
デフォルトは平日 13:30 UTC = 米国夏時間で 9:30 ET(市場寄り付き) = 22:30 JST です。
米国が冬時間(標準時)になると、同じ 9:30 ET は 23:30 JST になります。
好みの時間に変更する場合は cron 式を書き換えてください
(例: 引け後にまとめて見たい場合は 21:00 UTC ≒ 米国市場引け直後)。

`Actions` タブから `workflow_dispatch` で手動実行して、動作確認もできます。

### 4. カスタマイズ

`scanner.py` 冒頭の設定値で調整できます:

- `FUNDAMENTALS_STAGE_TOP_N`: 詳細分析(決算日・アナリスト評価など)まで進める候補数
- `DAY_TRADE_LIST_SIZE` / `LONG_TERM_LIST_SIZE`: メールに載せる件数
- `MIN_PRICE` / `MIN_AVG_DOLLAR_VOLUME`: 除外する低位株・低流動性株の基準

## スコアの算出方法(概要)

- **デイトレード向け利益期待スコア**: ATR%(値幅)・相対出来高・当日騰落率を加重合算
- **デイトレード向けリスクスコア**: 値幅・RSIの偏り(買われすぎ/売られすぎ)・出来高の薄さ
- **長期向け利益期待スコア**: 50日線/200日線上か・1ヶ月/3ヶ月モメンタム・アナリスト目標株価との乖離
- **長期向けリスクスコア**: 下降トレンドか・RSIの偏り・時価総額(小型株ほど加点)

いずれも `scanner.py` にロジックがそのまま書かれているので、必要に応じて重み付けを調整してください。

## ローカルでのテスト実行

```bash
pip install -r requirements.txt
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
export GMAIL_TO="you@gmail.com"
python scanner.py
```
