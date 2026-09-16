# 為替・コモディティ 急変動アラートシステム（クラウド版 / GitHub Actions）

Macを一切使わず、**GitHub Actions**（GitHubの無料の自動実行機能）だけで動く構成です。
Macの電源・スリープ・ログイン状態とは完全に無関係に、5分おきにクラウド上で価格をチェックし、
- **日中**: LINEに通知（1通）
- **夜間**: LINEを連続送信（バースト送信）
します。

これまでMacで使っていた `.command` ファイルやlaunchdの設定はもう不要です。

---

## 全体像

```
あなたのGitHubリポジトリ
├─ fx_commodity_alert.py      ← 本体（1回実行して終了するタイプ）
├─ requirements.txt
├─ state/
│   ├─ control.json           ← 「今日はオフ」「夜間通知の本数・間隔」を保存
│   └─ state.json              ← 直近の通知時刻を保存（クールダウン管理）
└─ .github/workflows/
    ├─ monitor.yml             ← 5分おきに自動実行（本体）
    └─ toggle.yml               ← 手動実行で設定変更（オフ/オン、夜間設定）
```

GitHub Actionsが5分おきに `fx_commodity_alert.py` を実行し、結果（通知履歴）を
`state/state.json` に書き戻してリポジトリにコミットします。これにより、次の実行でも
「さっき通知したばかりだから今は送らない」といったクールダウン判定が引き継がれます。

---

## 1. GitHubアカウント・リポジトリの準備

1. https://github.com/ でアカウントを作成（既にお持ちならログイン）
2. 右上の「+」→「New repository」で新規リポジトリを作成
   - リポジトリ名: 任意（例: `fx-alert`）
   - **Public（公開）を推奨**します。Public repoはGitHub Actionsの実行時間が無制限無料だからです。
     コード自体（監視銘柄やしきい値のロジック）が公開されるだけで、LINEトークンなどの
     秘密情報は後述のSecretsに保存するため公開されません。
     もしどうしてもPrivateにしたい場合は、本書末尾の「Privateにする場合の注意」を参照してください。
3. 「Create repository」をクリック

---

## 2. ファイルをリポジトリにアップロード

パソコンにGit未インストールの場合は先に入れてください（Macなら `xcode-select --install` でも入ります）。

ダウンロードしたこのフォルダ一式をお使いのMac（あるいはどのPCでも構いません）の
好きな場所に置き、ターミナルで以下を実行します（`YOUR_USERNAME/fx-alert` の部分は、
実際に作成したリポジトリのURLに置き換えてください。GitHubのリポジトリページの
緑色「Code」ボタンから確認できます）。

```bash
cd fx_alert_cloud
git init
git add .
git commit -m "初回コミット"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/fx-alert.git
git push -u origin main
```

GitHubのユーザー名・パスワード（正確にはPersonal Access Token）の入力を求められることがあります。
その場合は https://github.com/settings/tokens から発行したトークンをパスワード代わりに使うか、
GitHub Desktop（GUIアプリ）を使うと簡単です。

---

## 3. LINEトークンをSecretsに登録

1. 作成したリポジトリのページで「Settings」タブを開く
2. 左メニュー「Secrets and variables」→「Actions」
3. 「New repository secret」をクリック
4. Name: `LINE_CHANNEL_ACCESS_TOKEN`
   Value: これまで取得したチャネルアクセストークン（長期）を貼り付け
5. 「Add secret」で保存

これでコード上には一切トークンが登場しないまま、GitHub Actionsの実行時だけ
安全に読み込まれるようになります。

---

## 4. 動作確認

1. リポジトリの「Actions」タブを開く
2. 左側に「為替・コモディティ監視」というワークフローが表示されているはずです
3. それをクリックし、右側の「Run workflow」ボタン（手動実行）を押す
4. 実行が始まったら、実行中のジョブをクリックしてログを確認
   - 「LINE通知を送信しました」等のログが出ていればOK
   - しきい値を超えていない場合は何も送信されず、価格のログだけが出ます（正常です）
5. スマホのGitHubアプリを入れておくと、外出先でもActionsタブから同じ確認ができます

テスト送信だけしたい場合は、`fx_commodity_alert.py` をローカルで以下のように実行することもできます。

```bash
export LINE_CHANNEL_ACCESS_TOKEN="実際のトークン"
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python fx_commodity_alert.py --test-line
```

---

## 5. 自動実行の仕組み

`.github/workflows/monitor.yml` に設定した通り、**5分おきに自動で実行されます**。
何もしなくてもこのまま動き続けます。Macを閉じても、電源を切っても、スリープしても
一切影響ありません（GitHub側のサーバーで動いているため）。

> ⚠ GitHubの仕様上、スケジュール実行は「ぴったり5分おき」ではなく、数分程度前後することがあります。
> また、リポジトリに60日間まったく変化がないと自動実行が止まる仕様がありますが、
> 本システムは毎回 `state/state.json` を更新してコミットするため、通常はこれに該当しません。

---

## 6. 日常の使い方：オン・オフ・夜間設定の変更

Mac側の `.command` ファイルの代わりに、**GitHubの「Actions」タブから操作**します。

1. リポジトリの「Actions」タブ →「設定変更（今日はオフ／夜間通知の本数・間隔）」を選ぶ
2. 右側の「Run workflow」を押すと、入力欄が開きます
   - `off_today` にチェック: 今日だけ監視オフ
   - `off_until` に日付を入力（例: `2026-09-20`）: その日までオフ
   - `turn_on` にチェック: オフを解除
   - `night_count` / `night_interval`: 夜間LINEバーストの通数・間隔を変更
3. 必要な項目だけ入力して「Run workflow」

スマホのGitHubアプリからも同じ操作ができるので、外出先でも「今日はオフ」の切り替えが可能です。

現在の状態を確認したい場合は、`state/control.json` をGitHub上で直接開いて見ることもできますし、
「為替・コモディティ監視」ワークフローの実行ログにも毎回モード（日中/夜間）が出力されます。

---

## 7. しきい値・監視銘柄を変更する

`fx_commodity_alert.py` をGitHub上で直接編集できます。

1. リポジトリでこのファイルを開き、鉛筆マーク（Edit）をクリック
2. 冒頭の `CONFIG` 内、`default_threshold_pct` や `targets` を編集
3. 下の方にある「Commit changes」で保存

保存した瞬間から、次回の定期実行（最大5分後）にはもう新しい設定が使われます。

---

## 8. （オプション）電話発信機能

Twilioを使った電話発信も、環境変数経由でクラウド版に対応済みです。有効化する場合:

1. `fx_commodity_alert.py` の `CONFIG` 内 `call_enabled` を `True` に変更してコミット
2. GitHub Secretsに以下を追加（手順3と同じ場所）
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_FROM_NUMBER`
   - `TWILIO_TO_NUMBER`

---

## 9. Macの旧設定を後片付けする

以前Mac側で登録したlaunchdの設定が残っていると、**同じアラートが二重に届く**ことがあります。
念のため以下で削除しておいてください。

```bash
# LaunchAgent版を設定していた場合
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.fxalert.monitor.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/com.fxalert.monitor.plist

# LaunchDaemon版を設定していた場合（sudoが必要）
sudo launchctl bootout system /Library/LaunchDaemons/com.fxalert.monitor.plist 2>/dev/null
sudo rm -f /Library/LaunchDaemons/com.fxalert.monitor.plist
```

MacのスリープやAutomatic Loginの設定は、他に困ることがなければ元に戻して構いません
（このシステムのためにはもう必要ありません）。

---

## Privateリポジトリにする場合の注意

GitHubの無料プランは、Privateリポジトリの場合 **月2,000分のActions実行時間** という上限があります。
5分おきの実行（1日288回）だと、1回あたりの実行時間次第では上限を超える可能性が高いです。
Privateにしたい場合は `monitor.yml` の cron を `*/15` や `*/30`（15分・30分おき）に緩めることを
推奨します（気づく速度は少し落ちます）。Publicのままなら実行時間は無制限無料なので、この心配は不要です。

---

## ファイル一覧

| ファイル | 役割 |
|---|---|
| `fx_commodity_alert.py` | 本体スクリプト（1回実行タイプ） |
| `requirements.txt` | 必要なPythonパッケージ |
| `state/control.json` | 一時停止・夜間設定（自動更新） |
| `state/state.json` | 直近の通知時刻（自動更新、クールダウン管理用） |
| `.github/workflows/monitor.yml` | 5分おきの自動監視ワークフロー |
| `.github/workflows/toggle.yml` | オン/オフ・夜間設定を変更する手動ワークフロー |
| `.gitignore` | venv等をリポジトリに含めないための設定 |
