"""Test case to capture and display real Copilot WebSocket traffic for comparison."""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.auth import DEFAULT_PROFILE_DIR

def run_compare():
    print("=== Starting Copilot WebSocket Protocol Comparison Test ===")
    profile_path = Path(DEFAULT_PROFILE_DIR).resolve()
    
    with sync_playwright() as pw:
        # Launch browser with the same signed-in profile
        context = pw.chromium.launch_persistent_context(
            str(profile_path),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        
        captured_frames = []
        
        def on_ws(ws):
            # We are interested in the chat WebSocket. 
            print(f"\n[WS Connected] -> {ws.url}\n")
            
            def log_sent(payload):
                text = payload if isinstance(payload, str) else payload.decode('utf-8', 'replace')
                # Try to format JSON for readability
                try:
                    data = json.loads(text)
                    formatted = json.dumps(data, indent=2, ensure_ascii=False)
                    print(f"================ [SENT JSON] ================\n{formatted}")
                    captured_frames.append(("SENT", data))
                except Exception:
                    print(f"[SENT RAW] -> {text[:200]}")
                    captured_frames.append(("SENT_RAW", text))
                    
            def log_recv(payload):
                text = payload if isinstance(payload, str) else payload.decode('utf-8', 'replace')
                try:
                    data = json.loads(text)
                    # Only print significant events (not just ping/pong)
                    event = data.get("event") or data.get("type")
                    if event in ("appendText", "done", "challenge", "error", "challengeResponse"):
                        formatted = json.dumps(data, indent=2, ensure_ascii=False)
                        print(f"================ [RECV JSON ({event})] ================\n{formatted}")
                    captured_frames.append(("RECV", data))
                except Exception:
                    pass

            ws.on("framesent", log_sent)
            ws.on("framereceived", log_recv)
            
        page.on("websocket", on_ws)
        
        print("Navigating to Copilot...")
        page.goto("https://copilot.microsoft.com/", wait_until="domcontentloaded")
        print("\n[INSTRUCTION] Please manually turn on 'Deep Thinking' / 'Search' in the browser window, type a message, send it, and watch the WS logs here.")
        input("\nPress [Enter] in this terminal when you have captured the traffic and wish to close the browser...")
        context.close()
        
    print("\n=== Test Finished ===")
    
if __name__ == "__main__":
    import json
    run_compare()
