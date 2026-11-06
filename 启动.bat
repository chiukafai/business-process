@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   业务登记协同系统
echo ========================================
echo.
echo 启动后请用浏览器打开:  http://127.0.0.1:5000
echo 默认账号: admin   默认密码: Hailu@2026
echo 关闭此窗口即停止服务
echo.
python app.py
pause
