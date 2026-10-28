# Where the bot runs

**Default: your Mac.** `./install.sh` sets up a LaunchAgent: the bot listens from login until the Mac sleeps
or shuts down, and on wake picks up what you sent meanwhile (Telegram keeps it 24 hours).

**Optional: a free Google Cloud VM**, if you want the bot and the questions (`?`) answered at any hour, with
the Mac off. Same code, same instance folder, same `./install.sh`: on Linux it installs a systemd service
instead of the LaunchAgent.

One rule: **one place at a time**. Two copies listening on the same bot steal messages from each other
(Telegram answers the second with "409 Conflict"). Stop one before starting the other, and move the
instance's `cache/` along: it holds the Telegram offset, the group topics, the spending and the local copy
of INBOX.

| | Mac | Google Cloud e2-micro |
|---|---|---|
| Cost | free | free within the Always Free limits ([details](https://docs.cloud.google.com/free/docs/free-cloud-features)), card required |
| When it answers | while the Mac is on | always |
| Instagram/TikTok | work | may be blocked* |
| Effort | `./install.sh` | about an hour, once |

\* Instagram and TikTok often block data-center addresses. The page is still created, with the page preview
and the Error field, but without a transcript. Texts, web links, voice messages and questions work
everywhere. Step 3 below checks this before you switch.

LLM costs do not depend on where the bot runs.

## Google Cloud, step by step

The commands with `gcloud` run on your Mac (install it with `brew install --cask google-cloud-sdk`, then
`gcloud init`); the others run on the VM. Replace `instances/mine` with your instance folder.

### 1. Create the VM

In the [Cloud Console](https://console.cloud.google.com) create a project with billing enabled, then:

```sh
gcloud compute instances create inbox --zone=us-central1-a --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=30GB --boot-disk-type=pd-standard
```

Stay within the free limits: one e2-micro, region us-west1, us-central1 or us-east1, at most 30 GB of
*standard* disk. Then set a budget alert (*Billing, Budgets & alerts*, e.g. 1 €): you get an email if
something starts costing money. Check the first monthly invoice: the free tier pages do not say whether the
VM's public IPv4 address is free, and the bot needs one to reach Telegram.

### 2. Prepare it

```sh
gcloud compute ssh inbox --zone=us-central1-a
```
then, on the VM:
```sh
sudo timedatectl set-timezone Europe/Rome      # your time zone: the weekly digest goes by the local day
sudo apt-get update && sudo apt-get install -y python3-venv ffmpeg
# 1 GB of swap: the VM has 1 GB of RAM, enough for the bot but tight for pip and ffmpeg
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
exit
```

### 3. Copy the code and check videos

From the code folder on your Mac (the instance is copied in step 4):
```sh
COPYFILE_DISABLE=1 tar czf - --exclude .venv --exclude __pycache__ --exclude .DS_Store --exclude instances \
  --exclude cache --exclude logs . | gcloud compute ssh inbox --zone=us-central1-a -- \
  'mkdir -p ~/telegram-notion-inbox && tar xzf - -C ~/telegram-notion-inbox'
gcloud compute ssh inbox --zone=us-central1-a
```
on the VM:
```sh
cd ~/telegram-notion-inbox && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/yt-dlp -F "<a Reel link>"                           # a list of formats: Instagram works
.venv/bin/yt-dlp -F --impersonate chrome "<a TikTok link>"    # same for TikTok
```
An error such as "login required" or "rate-limit" means that platform blocks the VM: decide whether the
rest is worth it before going on. Nothing has changed on the Mac so far.

### 4. Move the bot

On the Mac: stop the bot, then copy the instance folder with its cache (logs stay behind).
```sh
./uninstall.sh <your label>       # the one install.sh printed, e.g. com.telegram-notion-inbox.mine
COPYFILE_DISABLE=1 tar czf - --exclude logs --exclude run.lock --exclude .DS_Store instances/mine | \
  gcloud compute ssh inbox --zone=us-central1-a -- 'tar xzf - -C ~/telegram-notion-inbox'
gcloud compute ssh inbox --zone=us-central1-a
```
on the VM:
```sh
cd ~/telegram-notion-inbox && ./install.sh instances/mine <your label>
journalctl -u <your label> -f      # live log (also in instances/mine/logs/bot.log); Ctrl+C to leave
```
Send the bot a message and a `?` question: both should be answered within seconds.

The service starts at boot and restarts after errors and after the weekly yt-dlp upgrade.
`sudo systemctl status <your label>` shows whether it is running.

### Updating the code

Repeat the first command of step 3 (it never touches `instances/`), then on the VM
`.venv/bin/pip install -r requirements.txt && ./install.sh instances/mine <your label>`.
If the code is on GitHub, `git pull` on the VM does the same.

### Back to the Mac

On the VM: `./uninstall.sh <your label>`. On the Mac, bring the cache back and start the LaunchAgent:
```sh
gcloud compute ssh inbox --zone=us-central1-a -- 'tar czf - -C ~/telegram-notion-inbox/instances/mine cache' | \
  tar xzf - -C instances/mine
./install.sh instances/mine <your label>
```
The VM can stay (free) or go: `gcloud compute instances delete inbox --zone=us-central1-a`.

### Notes

- Free outbound traffic is 1 GB a month. Text, transcripts and LLM calls use little; photos, voice messages
  and videos saved in Notion (`upload_files`) count in full. If you forward many videos, set
  `upload_files = false` or watch the billing page.
- Any Linux machine works the same way, e.g. a Raspberry Pi at home (which, like the Mac, does not get
  blocked by Instagram and TikTok): steps 2 to 4 without `gcloud`.

## Other option: GitHub Actions

`.github/workflows/bot.yml` runs `python main.py --once` every hour, free on a public repository. Items are
saved up to an hour late and questions answered at the next run, so it fits only if you do not use the agent.
It does nothing until you set the repository variable `BOT_ENABLED` to `true`; settings come from secrets
(`INSTANCE_TAR` from `scripts/instance_secret.sh instances/mine`, or `CONFIG_TOML`, `VOICE_MD` and one secret
per `.env` value). Scheduled workflows pause after 60 days without commits.
