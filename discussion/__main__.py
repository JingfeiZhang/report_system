"""Optional lightweight launcher for development; the main app exposes the same routes."""
import os
from pathlib import Path

from flask import Flask

from .api import register_discussion

root = Path(__file__).resolve().parents[1]
app = Flask(__name__, root_path=str(root), template_folder="templates", static_folder="static")
register_discussion(app)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("DISCUSSION_PORT", "5010")), threaded=True, debug=False)

