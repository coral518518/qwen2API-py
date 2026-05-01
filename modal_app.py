import modal
import os
import sys

# Define the Modal App
app = modal.App("qwen2api")

# Define a Persistent Volume so that config files (accounts.json, etc.) and
# generated files survive container restarts.
data_vol = modal.Volume.from_name("qwen2api-data", create_if_missing=True)

# Build the runtime image:
# 1. Start from Debian Slim Python 3.12 (matching your Dockerfile)
# 2. Install all OS dependencies needed for Camoufox headless browser logic
# 3. Pip install standard backend requirements
# 4. Fetch the Camoufox browser binary into the image cache
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "ca-certificates", "curl", "wget", "libasound2", "libatk-bridge2.0-0",
        "libatk1.0-0", "libcups2", "libdbus-1-3", "libdbus-glib-1-2", "libdrm2",
        "libgbm1", "libglib2.0-0", "libgtk-3-0", "libnspr4", "libnss3",
        "libpangocairo-1.0-0", "libpulse0", "libx11-6", "libx11-xcb1", "libxcb1",
        "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3", "libxkbcommon0",
        "libxrandr2", "libxshmfence1", "fonts-liberation", "fonts-noto-cjk"
    )
    .pip_install_from_requirements("backend/requirements.txt")
    .run_commands("python -m camoufox fetch")
    .add_local_dir("backend", remote_path="/workspace/backend")
    .add_local_dir("frontend/dist", remote_path="/workspace/frontend/dist")
)

# Export our ASGI web app instance
@app.function(
    image=image,
    # Hook our data volume so state is preserved
    volumes={"/workspace/data": data_vol},
    min_containers=0,
    cpu=0.3,
    memory=1024,
    timeout=200, # 200秒超时限制
    scaledown_window=120,
    secrets=[modal.Secret.from_name("qwen")],
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def serve():
    # Make sure we add /workspace into the python module resolution path
    import sys
    sys.path.append("/workspace")

    # Override the WORKSPACE base directory for Data files persistence
    os.environ["DATA_DIR"] = "/workspace/data"
    
    # Import the FastAPI Instance
    from backend.main import app as web_app
    return web_app

# 这个函数每 1 分钟运行一次，仅在白天 09:00 - 24:00（UTC+8）期间运行
# 注意：Modal 时间通常是 UTC，北京时间 09:00-24:00 对应 
@app.function(schedule=modal.Cron("*/1 1-16 * * *")) 
def keep_warm():
    import urllib.request
    
    # 动态获取当前部署的 webhook 地址
    # 不同 Modal 版本的 API 可能不同，做一层安全获取
    base_url = None
    if hasattr(serve, "get_web_url"):
        base_url = serve.get_web_url()
    elif hasattr(serve, "web_url"):
        base_url = serve.web_url
        
    if not base_url:
        print("未获取到 webhook url，请检查 Modal 版本或使用固定 URL。")
        return
        
    url = f"{base_url.rstrip('/')}/api"
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
        print(f"已触发内部保活... 请求地址: {url}")
    except Exception as e:
        print(f"保活请求出现异常 (服务可能仍在唤醒中): {e}")