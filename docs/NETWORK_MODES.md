# スマホ閲覧と接続管理（フルアクセス版）

Pythonモバイル依存はrequirements-mobile.txtを使用します。スマホ閲覧は読取り専用です。
本体をStart.cmdで起動後、scripts/Start-Network.ps1 -OpenUIでlocalhost:8767を開きます。
モバイルルーター・ホームルーターでは既存Windows Wi-Fiプロファイルを登録します。
ローカルエリアはWindows標準ホットスポットを使い、OS設定でSSIDとパスワードを確認します。
明示的な切替では閲覧サーバー8766を専用IP/インターフェース/送信元範囲へ限定し、必要なUAC承認を求めます。
スマホを同じWi-Fiへ接続し、Surfaceの接続案内QRで閲覧します。QR・閲覧キーは共有しません。
完全オフラインでのホットスポット起動、スリープ復帰、各端末の適合は導入先で検証します。
接続管理の変更中もBMSの本体記録を別プロセスで保持します。failed/recovery_requiredは実接続も確認してください。
比較診断は停車中に端末・経路・アダプター・モーターを記録し120秒観測します。
保存JSONの解析はpython -m gtbms.delivery_analysis input.json --output summary.jsonで実施できます。
HTTP往復・DOM反映・受信値の年齢推定は別の指標で、片道無線遅延や画面描画完了とは同一視しません。
この公開手順に個別導入先の測定結果は含めません。
