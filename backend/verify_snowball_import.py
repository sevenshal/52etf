import sys
import os

# Add the project root to sys.path
sys.path.append(os.getcwd())

try:
    from src.app.api.snowball import router, SnowballConfigCreate, SnowballLogResponse
    print("Verification Success: Import successful.")
except Exception as e:
    print(f"Verification Failed: {e}")
    import traceback
    traceback.print_exc()
