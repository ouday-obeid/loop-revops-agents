"""Main Slack Bolt application — listens for file uploads and dispatches
daily digest or full workbook generation."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

# Ensure the project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_schema import load_config
from core.loader import load_csv
from core.processor import Processor
from core.forecast_loader import load_forecast, load_forecasts
from core.deal_matcher import match_deals
from core.roster_loader import load_roster, apply_roster
from slack_bot.digest import generate_digest
from slack_bot.generator import generate_workbook
from slack_bot.movers import detect_movers, load_snapshot, save_snapshot

# Load environment variables from slack_bot/.env or project root .env
load_dotenv(Path(__file__).parent / ".env")
load_dotenv(PROJECT_ROOT / ".env")

CONFIG_PATH = os.environ.get("CONFIG_PATH", str(PROJECT_ROOT / "config.yaml"))

# Persistent location for the latest forecast docs and roster
FORECAST_DATA_DIR = Path(__file__).parent / "data"
FORECAST_SAVE_PATH = FORECAST_DATA_DIR / "forecast.xlsx"  # legacy single-file
ROSTER_SAVE_PATH = FORECAST_DATA_DIR / "roster.xlsx"

# App is created lazily in main() after token validation
app: App | None = None


def _file_extension(name: str) -> str:
    """Return lowercased file extension (e.g. '.csv')."""
    return Path(name).suffix.lower()


def _is_csv(name: str) -> bool:
    return _file_extension(name) == ".csv"


def _is_excel(name: str) -> bool:
    return _file_extension(name) in (".xlsx", ".xls")


def _is_roster(name: str) -> bool:
    """Detect roster files by name pattern (e.g. 'Sales Team Roster')."""
    lower = name.lower()
    return "roster" in lower and _is_excel(name)


def _download_file(client: WebClient, file_info: dict, dest: Path) -> Path:
    """Download a Slack file to a local path."""
    import urllib.request

    url = file_info.get("url_private_download") or file_info.get("url_private")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {client.token}"})
    with urllib.request.urlopen(req) as resp:
        dest.write_bytes(resp.read())
    return dest


def _load_forecast_into_proc(proc: Processor, cfg, forecast_paths: list[Path]) -> bool:
    """Load forecast doc(s) and attach to processor. Returns True on success."""
    try:
        if len(forecast_paths) == 1:
            forecast_data = load_forecast(str(forecast_paths[0]), cfg)
        else:
            forecast_data = load_forecasts([str(p) for p in forecast_paths], cfg)
        matched = match_deals(forecast_data, proc.df)
        proc.rep_forecast = {"data": forecast_data, "matched_deals": matched}
        return True
    except Exception as e:
        print(f"WARNING: Could not load forecast doc(s): {e}", file=sys.stderr)
        return False


def _process_files(channel: str, files: list[dict], client: WebClient, thread_ts: str):
    """Process uploaded files: load CSV, load forecast (new or saved), send digest + workbook."""
    csv_files = [f for f in files if _is_csv(f["name"])]
    roster_files = [f for f in files if _is_roster(f["name"])]
    excel_files = [f for f in files if _is_excel(f["name"]) and not _is_roster(f["name"])]

    if not csv_files:
        # If only a roster file was uploaded, save it and acknowledge
        if roster_files:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                dest = tmpdir / roster_files[0]["name"]
                _download_file(client, roster_files[0], dest)
                FORECAST_DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(dest), str(ROSTER_SAVE_PATH))
                print(f"Roster saved: {roster_files[0]['name']}")
            client.chat_postMessage(
                channel=channel,
                text="\u2705 Sales Team Roster updated. It will be used on the next CSV upload.",
            )
            return

        client.chat_postMessage(
            channel=channel,
            text="\u26a0\ufe0f I need at least a CSV file (Salesforce export). "
                 "Upload a single CSV for a daily digest, or CSV + Excel forecasting doc "
                 "for full workbook generation.",
        )
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Download CSV
        csv_local = tmpdir / csv_files[0]["name"]
        _download_file(client, csv_files[0], csv_local)

        # Download roster files
        roster_locals = []
        for rf in roster_files:
            dest = tmpdir / rf["name"]
            _download_file(client, rf, dest)
            roster_locals.append(dest)

        # Download all other Excel files (forecast docs)
        excel_locals = []
        for ef in excel_files:
            dest = tmpdir / ef["name"]
            _download_file(client, ef, dest)
            excel_locals.append(dest)

        try:
            cfg = load_config(CONFIG_PATH)

            # --- Load roster (new upload or saved) ---
            if roster_locals:
                FORECAST_DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(roster_locals[0]), str(ROSTER_SAVE_PATH))
                roster_data = load_roster(str(roster_locals[0]), cfg)
                apply_roster(cfg, roster_data)
                print(f"Roster saved and loaded: {roster_locals[0].name}")
            elif ROSTER_SAVE_PATH.exists():
                roster_data = load_roster(str(ROSTER_SAVE_PATH), cfg)
                apply_roster(cfg, roster_data)
                print("Using saved roster")

            df = load_csv(str(csv_local), cfg)
            proc = Processor(df, cfg)

            # --- Load forecast doc(s) ---
            forecast_loaded = False
            if excel_locals:
                # Save all uploaded forecast docs for future runs
                FORECAST_DATA_DIR.mkdir(parents=True, exist_ok=True)
                # Clear old saved forecasts when new ones are uploaded
                for old in FORECAST_DATA_DIR.glob("forecast_*.xlsx"):
                    old.unlink()
                saved_paths = []
                for i, el in enumerate(excel_locals):
                    save_name = f"forecast_{i}.xlsx" if len(excel_locals) > 1 else "forecast.xlsx"
                    save_path = FORECAST_DATA_DIR / save_name
                    shutil.copy2(str(el), str(save_path))
                    saved_paths.append(save_path)
                # Also keep legacy single-file path for backward compat
                if len(excel_locals) == 1:
                    shutil.copy2(str(excel_locals[0]), str(FORECAST_SAVE_PATH))
                forecast_loaded = _load_forecast_into_proc(proc, cfg, excel_locals)
                if forecast_loaded:
                    names = ", ".join(el.name for el in excel_locals)
                    print(f"Forecast docs saved and loaded: {names}")
            else:
                # Use previously saved forecast docs
                saved = sorted(FORECAST_DATA_DIR.glob("forecast_*.xlsx"))
                if not saved and FORECAST_SAVE_PATH.exists():
                    saved = [FORECAST_SAVE_PATH]
                if saved:
                    forecast_loaded = _load_forecast_into_proc(proc, cfg, saved)
                    if forecast_loaded:
                        print(f"Using {len(saved)} saved forecast doc(s)")

            # --- Deal movers ---
            yesterday_df = load_snapshot()
            movers = detect_movers(df, yesterday_df)
            save_snapshot(df)

            # --- Generate revenue model workbook ---
            wb_path = generate_workbook(proc=proc, cfg=cfg)

            # --- Upload workbook and get permalink ---
            upload_resp = client.files_upload_v2(
                channel=channel,
                file=str(wb_path),
                filename="revenue_model.xlsx",
                title="Revenue Model Workbook",
            )
            file_link = None
            try:
                file_obj = upload_resp.get("file") or {}
                if not file_obj:
                    files_list = upload_resp.get("files", [])
                    if files_list:
                        file_obj = files_list[0]
                file_id = file_obj.get("id")
                if file_id:
                    info = client.files_info(file=file_id)
                    file_link = info.get("file", {}).get("permalink")
            except Exception:
                pass

            # --- Build and send digest ---
            forecast_note = None
            if excel_locals and forecast_loaded:
                names = ", ".join(el.name for el in excel_locals)
                forecast_note = f"Forecast docs updated: {names}"
            elif forecast_loaded:
                forecast_note = "Using saved forecast doc"

            blocks = generate_digest(
                proc, cfg, movers,
                workbook_link=file_link,
                forecast_note=forecast_note,
            )
            client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text="Pipeline Update",
            )

        except Exception as e:
            client.chat_postMessage(
                channel=channel,
                text=f"\u274c Error processing files: {e}\n\n"
                     f"Make sure you're uploading a valid Salesforce CSV export.",
            )


def _register_handlers(app_instance: App):
    """Register all event handlers on the app instance."""

    @app_instance.event("file_shared")
    def handle_file_shared(event: dict):
        """Acknowledge file_shared events (subscribed by old FDD bot config)."""
        pass

    @app_instance.command("/fdd-search")
    def handle_fdd_search(ack, body):
        """Acknowledge old FDD slash command so it doesn't spam logs."""
        ack("This command is no longer active.")

    @app_instance.event("message")
    def handle_message(event: dict, client: WebClient):
        """Handle messages with file uploads in the channel."""
        # Ignore bot messages to prevent loops
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return

        files = event.get("files", [])
        if not files:
            return

        # Filter to CSV and Excel files only
        relevant = [f for f in files if _is_csv(f["name"]) or _is_excel(f["name"])]
        if not relevant:
            return

        channel = event["channel"]
        thread_ts = event.get("ts", "")

        # Process immediately — no collection window
        threading.Thread(
            target=_process_files,
            args=(channel, relevant, client, thread_ts),
            daemon=True,
        ).start()


def main():
    """Start the bot with Socket Mode (no public URL needed)."""
    global app

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")

    if not bot_token or bot_token.startswith("xoxb-your"):
        print("ERROR: SLACK_BOT_TOKEN not set (or still has placeholder value).", file=sys.stderr)
        print("  Edit slack_bot/.env with your real bot token from:", file=sys.stderr)
        print("  https://api.slack.com/apps → Your App → OAuth & Permissions", file=sys.stderr)
        sys.exit(1)
    if not app_token or app_token.startswith("xapp-your"):
        print("ERROR: SLACK_APP_TOKEN not set (or still has placeholder value).", file=sys.stderr)
        print("  Edit slack_bot/.env with your real app token from:", file=sys.stderr)
        print("  https://api.slack.com/apps → Your App → Basic Information → App-Level Tokens", file=sys.stderr)
        sys.exit(1)

    app = App(token=bot_token)
    _register_handlers(app)

    handler = SocketModeHandler(app, app_token)
    print("Bot connected — listening for file uploads...")
    handler.start()


if __name__ == "__main__":
    main()
