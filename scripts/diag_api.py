"""P1.1 诊断：绕过 vanna 直连 SiliconFlow，定位超时原因。不打印 Key。"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

key = None
env_path = ROOT / ".env"
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line.startswith("SILICONFLOW_API_KEY="):
        key = line.split("=", 1)[1].strip()
if not key or key == "your_key_here":
    sys.exit("Key 未就绪")

from openai import OpenAI

client = OpenAI(api_key=key, base_url="https://api.siliconflow.cn/v1", timeout=90, max_retries=0)

# 1) Key 有效性 + 端点连通（轻量调用）
print("测试1：列模型（验Key+连通）...")
t0 = time.time()
try:
    models = client.models.list()
    names = [m.id for m in models.data]
    print(f"  OK {time.time()-t0:.1f}s，模型数={len(names)}")
    hits = [n for n in names if "deepseek" in n.lower()]
    print("  deepseek 系:", hits[:8])
except Exception as e:
    print("  FAIL:", type(e).__name__, str(e)[:200])
    sys.exit(1)

# 2) 最小 chat 调用（测模型响应速度）
for model in ["deepseek-ai/DeepSeek-V3.2", "deepseek-ai/DeepSeek-V3"]:
    print(f"测试2：chat 调用 {model}（小prompt, 90s超时）...")
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复两个字：收到"}],
            max_tokens=16,
            temperature=0,
        )
        dt = time.time() - t0
        print(f"  OK {dt:.1f}s -> {r.choices[0].message.content!r}")
    except Exception as e:
        print(f"  FAIL {time.time()-t0:.1f}s:", type(e).__name__, str(e)[:200])
