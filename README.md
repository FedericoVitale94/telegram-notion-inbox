# telegram-notion-inbox

Forward anything to a private Telegram bot (links, Reels, TikToks, YouTube videos, voice messages,
texts, photos with captions) and find it sorted in a Notion database: classified and summarized,
with author and title for books and an estimated reading time for articles.

A personal inbox for whatever you save and never find again: articles and books to read, videos to
watch, guides, recipes, notes. Categories, tags and prompts are all in one config file.
If you publish content, the optional creator mode also scores your ideas and drafts scripts in your voice.

## What it does with each message

1. **Reads it**: text, links, forwarded posts, albums, long texts that Telegram split in two.
2. **Gets the content**: for Instagram, TikTok and YouTube it downloads the audio only (yt-dlp) and
   transcribes it with Whisper on Groq (free tier). Videos with no speech (music and on-screen text)
   get 4 frames looked at by Claude, on-screen text included. For web links it reads the page title
   and description; voice messages are transcribed too.
3. **Classifies it** with Claude Haiku: category, title, one-line topic, tags, summary. A hashtag in
   your message (`#recipe`) sets the category yourself.
4. **Adds what helps you use it later**:
   - books: title and author (pages in the book category take the book's title), plus year, pages, ISBN,
     publisher and cover from [Open Library](https://openlibrary.org), free and checkable;
   - web articles: estimated reading time, from the words of the page's main text (menus and footers
     excluded; behind a paywall only the free part is counted).
5. **Creator mode only, off by default** (`[creator]` in `config.toml`). For ideas (the category you choose):
   - checks whether you already published the same subject (your archive database in Notion, read-only)
     or already saved it (then the new link is added to the existing page instead of a duplicate);
   - gives a 1-5 score with a short reason;
   - from a score you choose, writes a script draft with Claude Opus, using your style rules and your
     best past scripts as examples, plus a list of facts to verify;
   - for the best ideas, searches the web and lists sources that confirm or contradict the key facts.
6. **Saves a page** in the Notion INBOX and replies on Telegram with category, title, author or reading
   time, score in creator mode, and link.

Once a week you get a Telegram digest of what is still "To review", a few articles to read sorted by
reading time, how many books are waiting and what the bot spent, and yt-dlp upgrades itself so
Instagram and TikTok downloads keep working. In creator mode the digest lists the best ideas instead,
and below the draft threshold you decide: set the page's Status to "Good idea" in Notion and the next
run writes the draft and tells you on Telegram.

If something fails (a blocked download, an API error) the page is still created with the Error field
filled, and one bad item never blocks the others. If a whole run fails you get a Telegram message.

## How it runs

No server and no webhook. On a Mac, `./install.sh` sets up a LaunchAgent that starts the bot at login with
`python main.py --listen`: it keeps polling Telegram while the computer is on, so items are saved and
questions answered within seconds. Every hour it writes requested drafts (creator mode), on the chosen
weekday it sends the digest, and once a week it upgrades yt-dlp and restarts itself to load it. While the
computer sleeps the bot is paused; on wake it picks up the queued messages (Telegram keeps them 24 hours).

To keep it on with the Mac off, the same `./install.sh` on Linux installs a systemd service: a free Google
Cloud e2-micro VM is enough. Step-by-step guide, and how to move back and forth: [docs/hosting.md](docs/hosting.md).
`python main.py --once` fetches the new messages, processes them and exits, for manual runs.

Telegram's update offset is confirmed only after an item is saved in Notion, so nothing is lost if a
run stops halfway. A lock file prevents two runs at once.

## In the chat and in a group

Use the bot in its private chat, or add it to a private group with Topics. In a topic, `/here saved` makes it
the one for the bot's replies, `/here alerts` the one for errors, and `/here <category>` (or one of its
hashtag aliases) sends everything written there to that category, and the replies for items of that category
land there too, so each such topic becomes the list of that category. The other topics collect items as usual.

Categories listed under `[categories.forced]` are never guessed by the classifier: you pick them with a
hashtag or a topic, and the item is saved as it is, titled with its first line, with no analysis. Handy for
notes to yourself, like ideas for improving the bot.

With `delete_saved = true` the bot deletes your messages once they are in Notion and analyzed, so the chat
only shows what is still waiting. Photos, files and voice messages are first saved in the Notion page
(up to 20 MB each). A message stays when something went wrong (an error, a file that could not be saved, the
spending limit reached), and questions to the agent stay too. Telegram lets a bot delete messages only within
48 hours, and in a group only if it is an admin with the right to delete messages.

## Talk to your inbox

A message that starts with `?` (or `/ask`) is not saved: Claude Sonnet answers it, reading your INBOX,
in the same chat. For example:

```
? which articles under 10 minutes are still to read
? what books did I save this month, with authors
? summarize the guide about sourdough and add a shopping list to its page
? mark the recipes saved before September as discarded
```

It searches a local copy of INBOX kept in sync with Notion (only what changed is read, plus a full re-read
once a day), so answers stay fast as the inbox grows. It can search and read INBOX pages, search the web (at most 3 searches per question, with sources),
change page statuses and add a section at the end of a page. In creator mode it can also search your
archive and write drafts (at most 2 per question). It writes only to INBOX and does not save new links:
send those without `?`.

Limits, set in `[agent]` in `config.toml`: 30 questions a day and 12 steps per question; more than 3
status changes at once need your confirmation. It remembers the last 6 questions of the chat for 2 hours,
so "yes, go ahead" or "and the ones about Rome?" work. Estimated cost at Sonnet 5.5 prices ($2 per million
input tokens, $10 output, the conversation already sent read from the cache at $0.10): about 1-3 cents for
a simple question, 5-15 cents for one that reads many pages; each web search adds 1 cent and each draft
costs what a draft costs. The log shows the tokens of every question. Model: `ANTHROPIC_MODEL_AGENT` in `.env`. Set `enabled = false` to turn it off.

## Setup

You need Python 3.11+ and ffmpeg (`brew install ffmpeg` on macOS).

1. **Telegram bot**: create it with [@BotFather](https://t.me/BotFather) and copy the token. Get your
   user ID from [@userinfobot](https://t.me/userinfobot). Only the IDs you list can use the bot.
2. **Notion**: create an internal integration at https://www.notion.so/profile/integrations
   (capabilities: read, update and insert content) and copy its token. Open the Notion page that will
   hold the INBOX, *••• menu, Connections*, and add the integration.
   If you use an archive database, share it with the integration too.
3. **API keys**: [Anthropic](https://platform.claude.com) (pay per use) and
   [Groq](https://console.groq.com) (free tier, for transcription).
4. **Install and configure**:
   ```sh
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env                        # tokens and keys
   cp config.example.toml config.toml          # brand, categories, tags, creator mode
   cp prompts/voice.example.md prompts/voice.md  # creator mode only: your style rules for drafts
   .venv/bin/python setup_notion.py <URL of the Notion page>   # creates INBOX, prints its ID for .env
   ```
5. **Try it**: send the bot a message, then `.venv/bin/python main.py --once`, or ask it something with `?`.
6. **Automate it**: `./install.sh` (LaunchAgent on macOS, systemd on Linux). Remove with `./uninstall.sh`.

`.env`, `config.toml` and `prompts/voice.md` are git-ignored: your keys and your brand stay local.

## Configuration

Everything about *you* lives in `config.toml` (see the comments in
[config.example.toml](config.example.toml)):

- **brand**: one sentence that every prompt starts from, and the output language;
- **categories**: yours, one line each (`name = "how to recognize it"`), plus the fallback and,
  optionally, the book category. Edit them any time: new names show up in Notion by themselves;
- **topics**: the tags;
- **reading**: words per minute and the minimum length for a reading time;
- **agent**: questions on/off, daily limit, steps and web searches per question;
- **ui**: the language of the texts the bot writes in Notion and on Telegram (en, it), and single texts;
- **notion**: the names of your INBOX properties and statuses, if they differ from the defaults;
- **telegram**: deleting saved messages, saving files in Notion, the group command and its words;
- **books**: Open Library on/off;
- **costs**: a monthly limit in euros (none by default) and prices;
- **creator**: creator mode on/off and the "idea" category. The settings below apply only when it is on;
- **archive** (optional): a Notion database of what you published, with the names of its properties.
  Used for the "already covered" check and, if it holds scripts and a performance number, to pick the
  best scripts as draft examples. The bot never writes to it;
- **drafts**: on/off, minimum score, word range, style file;
- **video**: TikTok browser impersonation, off by default (TikTok blocks automated downloads; turning
  it on is your call, check TikTok's terms).

Models: `ANTHROPIC_MODEL_CLASSIFY` and `ANTHROPIC_MODEL_DRAFT` in `.env`.
`LLM_PROVIDER=groq` switches classification and drafts to a free Groq model (lower quality).

## Costs

The bot records what each Anthropic call costs, at the prices in `costs.py` (or `[costs.prices]`), converted
to euros at the European Central Bank's daily rate. The weekly digest shows the last 7 days and the month.
With `monthly_limit_eur` set, you get an alert at 80% and at 100%; past the limit items are saved without
analysis and questions pause until the 1st.

- Telegram, Notion, yt-dlp, Groq transcription: free.
- Claude Haiku for classification and scoring: a fraction of a cent per item.
- Claude Haiku looking at frames of a speechless video: under a cent.
- Questions to the agent (Claude Sonnet): see [Talk to your inbox](#talk-to-your-inbox).
- Creator mode only, Claude Opus for drafts: roughly 5-10 cents per draft. Raise `drafts.min_score` or turn drafts off to
  spend less.
- Creator mode only, source search: a few cents per idea (Anthropic bills web searches on top of tokens). Only for ideas
  at `sources.min_score` or above.

## Your instance, and private customizations

The code is this folder; your data is an *instance folder*: `config.toml`, `.env`, `prompts/`, `custom.py`,
`cache/`, `logs/`. By default the instance is this folder. To keep it elsewhere (or to run more than one
bot), put those files in a folder of their own, for example `instances/mine/` (git-ignored), and point the
bot to it: `INBOX_HOME=instances/mine .venv/bin/python main.py --once`, or `./install.sh instances/mine`.

Everything personal stays in the instance folder and out of git:

- `prompts/<name>.md` replaces a built-in prompt: `classify`, `idea`, `sources`, `draft`, `visual`, `photo`, `agent`.
  Use the same `{placeholders}` as the built-in ones (see the `PROMPT` constants in the code);
- `custom.py` can replace steps with your own Python: `archive()` (what you published, e.g. joined from
  two databases), `draft_examples()`, `draft_postprocess(text)`, `draft_checks(text, lo, hi)`.
  It is loaded by its path, never from `sys.path`.

To run an instance on GitHub Actions instead, put the output of `scripts/instance_secret.sh instances/mine`
in a secret named `INSTANCE_TAR`: the workflow unpacks it and uses it as the instance folder.

## Safety

- Notion writes are limited in code to the INBOX database (`notion.py`): pages the bot created, or
  pages it checked belong to INBOX. The archive is only read. At the start of each run the bot adds to
  INBOX the properties it lacks (Book, Author, Reading time, and the creator ones if that mode is on),
  so an INBOX made by an older version keeps working; existing properties are never changed.
- Only Telegram users listed in `TELEGRAM_ALLOWED_USER_IDS` are processed, questions included.
- The agent's tools write only through the same INBOX-only functions; bulk status changes need confirmation.
- Tokens never appear in logs; downloaded audio is deleted right after transcription.

## Development

```sh
.venv/bin/python -m unittest discover -s tests
tail -f logs/bot.log
```

## License

MIT, see [LICENSE](LICENSE).
