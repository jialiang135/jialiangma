@echo off
REM ============================================
REM 个人数字分身 · Docker 启动脚本 (Windows)
REM ============================================

echo ============================================
echo   个人数字分身 ^· Docker 启动
echo ============================================

REM 切换到项目根目录
cd /d "%~dp0.."

REM 检查 config\.env 是否存在
if not exist config\.env (
    echo [WARNING] config\.env 不存在！
    echo           请复制 config\.env.example 或手动创建 config\.env 并填入 API Key。
    echo.
)

REM 检查必要的 API Key
REM 注意：batch 无法直接 source .env，仅做文件存在性提醒
echo [INFO] 请确保 config\.env 中已配置 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY。
echo.

echo ^>^>^> 构建并启动 Docker 容器...
docker compose build --pull
docker compose up -d

echo.
echo ============================================
echo   服务已启动！
echo   访问地址: http://localhost:7863
echo   API 文档: http://localhost:7863/api/docs
echo   健康检查: http://localhost:7863/api/health
echo ============================================
echo.
echo 查看日志: docker compose logs -f
echo 停止服务: docker compose down

pause
