# OSS調査・ライセンス

更新日: 2026-10-11（任意のローカルQR生成依存を追加）。BatMONについての2026-10-10調査では、公式READMEはHome Assistant向けアドオンに加え、Windows / macOS / Linuxでの単独実行を案内しています。[Standalone手順](https://github.com/fl4p/batmon-ha/blob/master/doc/Standalone.md)も公開されています。以前の「Windows対応の記載は確認していない」という調査結果を訂正しました。本アプリの継続監視はJBD読み取り部分を参考にした独立実装を維持し、追加のJBD接続認証を検証するため `aiobmsble` を任意の診断依存として利用します。互換性と認証方式の調査は [BATMON_COMPATIBILITY.md](docs/BATMON_COMPATIBILITY.md) を参照してください。

上流のJBDモジュールにはMOS切替機能もあります。今回の実装へは取り込みません。上流masterは変化するため、対象バッテリーでの実機確認を別の受入条件とします。

| OSS | 用途 | ライセンス | 確認元 |
|---|---|---|---|
| fl4p/batmon-ha | ネイティブJBDプロトコル参考 | MIT（別途LGPL-2.1通知あり） | [repository](https://github.com/fl4p/batmon-ha)、[JBD source](https://github.com/fl4p/batmon-ha/blob/master/bmslib/models/jbd.py)、[LICENSE](https://github.com/fl4p/batmon-ha/blob/master/LICENSE)、[LICENSE_BMS_BLE-HA](https://github.com/fl4p/batmon-ha/blob/master/LICENSE_BMS_BLE-HA) |
| aiobmsble 0.29.0 | 任意のJBD診断・接続認証コネクター | Apache-2.0 | [PyPI 0.29.0](https://pypi.org/project/aiobmsble/0.29.0/)、[LICENSE](https://github.com/patman15/aiobmsble/blob/main/LICENSE) |
| bleak-retry-connector 4.6.3 | 任意の診断依存 | MIT | [PyPI 4.6.3](https://pypi.org/project/bleak-retry-connector/4.6.3/)、導入distributionのmetadata・ライセンス |
| qrcode 8.2 | 任意のスマホ接続QR生成（ローカルSVG） | BSD-3-Clause（同梱由来コードのMIT通知あり） | [repository](https://github.com/lincolnloop/python-qrcode)、[PyPI 8.2](https://pypi.org/project/qrcode/8.2/)、導入distributionの `qrcode-8.2.dist-info/LICENSE` とmetadataを確認 |
| Bleak | Windows BLE/WinRT | MIT | [LICENSE](https://github.com/hbldh/bleak/blob/develop/LICENSE) |
| FastAPI | ローカルAPI | MIT | [LICENSE](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Uvicorn | ASGI実行 | BSD-3-Clause | [LICENSE](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) |
| psutil | CPU・メモリ監視 | BSD-3-Clause | [LICENSE](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| websockets | WebSocket | BSD-3-Clause | [LICENSE](https://github.com/python-websockets/websockets/blob/main/LICENSE) |
| SQLite/Python sqlite3 | ローカル保存 | SQLite public domain / Python PSF | [SQLite copyright](https://www.sqlite.org/copyright.html)、[Python license](https://docs.python.org/3/license.html) |
| pytest / pytest-asyncio / HTTPX | 開発テスト | MIT / Apache-2.0 / BSD-3-Clause | インストールdistributionのmetadata・ライセンス |

BatMONリポジトリにはBMS_BLE-HA由来コードのLGPL-2.1通知もあるため、リポジトリ全体を一律MITとして扱いません。本アプリはその同梱コードをコピーせず、PyPIの `aiobmsble==0.29.0` を任意依存として利用します。

依存パッケージはpipで導入し、各distribution内の著作権・ライセンスファイルを維持します。バイナリや仮想環境を再配布する場合は、任意依存と推移依存を含むライセンスも同梱してください。`scripts/Collect-Licenses.py`で現在環境のmetadataと同梱ライセンスを収集できます。

スマホ閲覧のサーバー自体に新しい必須依存はありません。`qrcode==8.2` は `requirements-mobile.txt` とprojectのoptional `mobile` にだけ追加し、インストールされていればローカルの接続案内にSVGのQRを生成します。Pillow等の画像生成extraは要求しません。未導入時はURLと閲覧キーで接続でき、外部QR生成サービスやCDNは使用しません。導入済み8.2のmetadataはBSDを示し、同梱LICENSEの3条件と元のpyqrnative／JavaScript由来のMIT通知を確認しました。再配布時はこの由来通知も保持してください。

## BatMON MIT notice

MIT License

Copyright (c) 2024 Fabian Schlieper

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## qrcode 8.2 distribution notice

Copyright (c) 2011, Lincoln Loop
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the package name nor the names of its contributors may be
      used to endorse or promote products derived from this software without
      specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE 
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


-------------------------------------------------------------------------------


Original text and license from the pyqrnative package where this was forked
from (http://code.google.com/p/pyqrnative):

#Ported from the Javascript library by Sam Curren
#
#QRCode for Javascript
#http://d-project.googlecode.com/svn/trunk/misc/qrcode/js/qrcode.js
#
#Copyright (c) 2009 Kazuhiko Arase
#
#URL: http://www.d-project.com/
#
#Licensed under the MIT license:
#   http://www.opensource.org/licenses/mit-license.php
#
# The word "QR Code" is registered trademark of
# DENSO WAVE INCORPORATED
#   http://www.denso-wave.com/qrcode/faqpatent-e.html
