@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo GT BMS データ移行
echo 旧アプリと復旧版を開かず、USB接続した端末のロックを解除してください。
echo 旧アプリを削除・初期化する処理は含まれていません。
echo.
py -3 -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0migrate.py"
  goto done
)
python -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if not errorlevel 1 (
  python "%~dp0migrate.py"
  goto done
)
echo Python 3.9以降が必要です。READMEの準備手順を確認してください。
:done
echo.
echo PASSの場合は復旧版を開いて記録を確認してください。
echo STOPの場合はアプリやバックアップを削除せず、表示内容をお知らせください。
pause
