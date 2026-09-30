import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
os.chdir(os.path.dirname(__file__))

import uvicorn

if __name__ == "__main__":
    print("旅图 TravelAgent → http://127.0.0.1:8000")
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000)
