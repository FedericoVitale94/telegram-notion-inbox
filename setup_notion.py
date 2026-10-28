"""Creates the INBOX database in Notion from config.toml.
Usage: python setup_notion.py <URL or ID of the Notion page that will contain it>
The page must be shared with your integration (••• menu, Connections)."""
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import config  # noqa: E402
import notion  # noqa: E402

if len(sys.argv) != 2 or not (m := re.search(r"[0-9a-f]{32}", sys.argv[1].replace("-", ""))):
    sys.exit(__doc__)
db_id = notion.create_inbox_database(m.group(), config.CATEGORIES + config.FORCED, config.TOPICS, config.CREATOR_ON)
print(f"INBOX created. Put this line in .env:\nNOTION_INBOX_DB_ID={db_id.replace('-', '')}")
