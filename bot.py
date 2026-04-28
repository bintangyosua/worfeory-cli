"""
Wordle automation bot for wordlecup.io
Uses Selenium + Chrome to play the game, and calls the Rust solver CLI for decisions.
Runs as a state machine that loops indefinitely across multiple games.

States:
  LOBBY         — Landing page with username input + "Play Game" button
  IN_GAME       — Game board is active, ready to guess
  WAITING       — Solved/failed, waiting for round timer or other players
  SCOREBOARD    — Scoreboard popup visible between rounds
  BETWEEN_GAMES — All rounds done, waiting for next game to start
"""

import subprocess
import time
import sys
import os
import re

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys


# ─── Configuration ────────────────────────────────────────────────────────────

SOLVER_PATH = os.path.join(os.path.dirname(__file__), "target", "release", "worfeory-cli.exe")
WORDLE_URL = "https://wordlecup.io"
FIRST_GUESS = "salet"
MAX_ATTEMPTS = 6
POLL_INTERVAL = 0.3     # seconds between state/DOM checks
USERNAME = ""           # leave empty to keep whatever is in the input




# ─── State Constants ──────────────────────────────────────────────────────────

STATE_LOBBY = "LOBBY"
STATE_IN_GAME = "IN_GAME"
STATE_WAITING = "WAITING"
STATE_SCOREBOARD = "SCOREBOARD"
STATE_BETWEEN_GAMES = "BETWEEN_GAMES"
STATE_UNKNOWN = "UNKNOWN"

# States the bot can act on (not just idle-poll)
ACTIONABLE_STATES = {STATE_LOBBY, STATE_IN_GAME, STATE_SCOREBOARD}



# ─── Helpers ──────────────────────────────────────────────────────────────────

def create_driver():
    """Create a Chrome WebDriver with a clean session."""
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    return driver


# ─── State Detection ─────────────────────────────────────────────────────────

def detect_state(driver):
    """Detect the current state of the game page.

    Checks DOM elements in priority order to determine which state we're in.
    Returns one of the STATE_* constants.
    """
    try:
        # 1. Check for scoreboard modal (highest priority — overlays everything)
        modals = driver.find_elements(By.CSS_SELECTOR, 'section.chakra-modal__content[role="dialog"]')
        for modal in modals:
            if modal.is_displayed():
                return STATE_SCOREBOARD

        # 2. Check for lobby — "Play Game" button visible
        buttons = driver.find_elements(By.CSS_SELECTOR, "button.chakra-button")
        for btn in buttons:
            try:
                if btn.is_displayed() and btn.text.strip() == "Play Game":
                    return STATE_LOBBY
            except:
                pass

        # 3. Check for in-game — game board + keyboard present
        board = driver.find_elements(By.CSS_SELECTOR, "table.Game-rows")
        keyboard = driver.find_elements(By.CSS_SELECTOR, "div.Game-keyboard")

        if board and keyboard:
            # Check if we're waiting (solved or "wait for others" text)
            page_text = driver.find_element(By.TAG_NAME, "body").text
            if "Please wait for others to finish" in page_text:
                return STATE_WAITING
            if "is correct" in page_text:
                return STATE_WAITING

            # Check if board has any empty rows left (can still play)
            rows = driver.find_elements(By.CSS_SELECTOR, "table.Game-rows tr.Row")
            has_empty_row = False
            for row in rows:
                cells = row.find_elements(By.CSS_SELECTOR, "td.Row-letter")
                if cells:
                    first_class = cells[0].get_attribute("class") or ""
                    first_text = cells[0].text.strip()
                    has_eval = any(x in first_class for x in [
                        "letter-correct", "letter-elsewhere", "letter-absent"
                    ])
                    if not has_eval and not first_text:
                        has_empty_row = True
                        break

            if has_empty_row:
                return STATE_IN_GAME
            else:
                # All rows filled/evaluated — waiting for round to end
                return STATE_WAITING

        # 4. Check for between-games state
        #    The page might show round info but no active board, or
        #    a countdown to the next game
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if "Next game" in page_text or "Waiting" in page_text:
            return STATE_BETWEEN_GAMES

    except Exception as e:
        pass

    return STATE_UNKNOWN


def get_round_info(driver):
    """Read current round and total from DOM text like 'Round 3/10'.

    Returns (current_round, total_rounds) or (None, None) if not found.
    """
    try:
        texts = driver.find_elements(By.CSS_SELECTOR, "p.chakra-text")
        for el in texts:
            t = el.text.strip()
            match = re.search(r"Round\s+(\d+)\s*/\s*(\d+)", t)
            if match:
                return int(match.group(1)), int(match.group(2))
    except:
        pass
    return None, None


def get_used_rows(driver):
    """Count how many rows have already been evaluated on the board."""
    count = 0
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table.Game-rows tr.Row")
        for row in rows:
            cells = row.find_elements(By.CSS_SELECTOR, "td.Row-letter")
            if cells:
                first_class = cells[0].get_attribute("class") or ""
                has_eval = any(x in first_class for x in [
                    "letter-correct", "letter-elsewhere", "letter-absent"
                ])
                if has_eval:
                    count += 1
                else:
                    break
    except:
        pass
    return count


# ─── Game Actions ─────────────────────────────────────────────────────────────

def click_play_game(driver):
    """Click the 'Play Game' button on the lobby screen."""
    buttons = driver.find_elements(By.CSS_SELECTOR, "button.chakra-button")
    for btn in buttons:
        try:
            if btn.is_displayed() and btn.text.strip() == "Play Game":
                # Optionally set username first
                if USERNAME:
                    try:
                        name_input = driver.find_element(
                            By.CSS_SELECTOR, 'input[placeholder="Choose a name"]'
                        )
                        name_input.clear()
                        name_input.send_keys(USERNAME)
                        time.sleep(0.3)
                    except:
                        pass
                btn.click()
                return True
        except:
            pass
    return False


def type_word(driver, word):
    """Type a word using the on-screen keyboard buttons.

    Uses JavaScript clicks to avoid ElementClickInterceptedException
    when overlays are partially covering the keyboard.
    """
    keyboard = driver.find_element(By.CSS_SELECTOR, "div.Game-keyboard")
    buttons = keyboard.find_elements(By.CSS_SELECTOR, "div.Game-keyboard-button")

    button_map = {}
    enter_btn = None
    for btn in buttons:
        text = btn.text.strip().lower()
        if text == "enter":
            enter_btn = btn
        elif len(text) == 1 and text.isalpha():
            button_map[text] = btn

    for ch in word.lower():
        if ch in button_map:
            driver.execute_script("arguments[0].click();", button_map[ch])
            time.sleep(0.05)
        else:
            print(f"  [WARN] Key '{ch}' not found on keyboard")

    time.sleep(0.1)
    if enter_btn:
        driver.execute_script("arguments[0].click();", enter_btn)
    else:
        print("  [ERROR] Enter button not found!")


def read_result(driver, row_idx):
    """Read the evaluation result from a specific row.

    Returns a string like '01020' (0=absent, 1=elsewhere, 2=correct),
    or None if the row has not been evaluated yet.
    """
    rows = driver.find_elements(By.CSS_SELECTOR, "table.Game-rows tr.Row")
    if row_idx >= len(rows):
        return None

    row = rows[row_idx]
    cells = row.find_elements(By.CSS_SELECTOR, "td.Row-letter")

    result = []
    for cell in cells:
        class_attr = cell.get_attribute("class") or ""
        if "letter-correct" in class_attr:
            result.append("2")
        elif "letter-elsewhere" in class_attr:
            result.append("1")
        elif "letter-absent" in class_attr:
            result.append("0")
        else:
            # Cell not yet evaluated
            return None

    return "".join(result)


def wait_for_result(driver, row_idx, timeout=10):
    """Poll until tile evaluation classes appear on the given row.

    Returns the result string, or None on timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        result = read_result(driver, row_idx)
        if result and len(result) == 5:
            return result
        time.sleep(0.1)
    return None


def call_solver(history):
    """Call the Rust solver CLI with cumulative guess/result history.

    Returns next guess word (string) or None on failure.
    """
    cmd = [SOLVER_PATH]
    for guess, result in history:
        cmd.extend([guess, result])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print(f"  [SOLVER ERROR] {proc.stderr.strip()}")
            return None
        word = proc.stdout.strip().lower()
        if len(word) == 5 and word.isalpha():
            return word
        print(f"  [SOLVER ERROR] Unexpected output: '{word}'")
        return None
    except subprocess.TimeoutExpired:
        print("  [SOLVER ERROR] Solver timed out")
        return None
    except FileNotFoundError:
        print(f"  [SOLVER ERROR] Solver not found at: {SOLVER_PATH}")
        return None


def display_result(guess, result):
    """Pretty-print the guess result with colored squares."""
    colors = {"2": "🟩", "1": "🟨", "0": "⬛"}
    tiles = "".join(colors.get(c, "?") for c in result)
    letters = " ".join(ch.upper() for ch in guess)
    print(f"          {tiles}")
    print(f"          {letters}")


# ─── Round Play ───────────────────────────────────────────────────────────────

def play_round(driver):
    """Play a single Wordle round.

    Returns True if solved, False if failed/error.
    """
    history = []
    guess = FIRST_GUESS

    # Figure out which row we start from (in case we joined mid-round)
    start_row = get_used_rows(driver)
    if start_row >= MAX_ATTEMPTS:
        print("  [INFO] All rows already used, skipping round")
        return False

    for attempt in range(start_row, MAX_ATTEMPTS):
        # Re-check state before each guess to avoid typing into an overlay
        current_state = detect_state(driver)
        if current_state not in (STATE_IN_GAME,):
            print(f"  [INFO] State changed to {current_state}, aborting round")
            return False

        print(f"  [{attempt + 1}/{MAX_ATTEMPTS}] guess={guess.upper()}")

        type_word(driver, guess)

        # Poll until tiles are evaluated (no fixed sleep)
        result = wait_for_result(driver, attempt, timeout=10)
        if not result:
            print(f"  [ERROR] Timed out reading result for row {attempt}")
            return False

        display_result(guess, result)

        if result == "22222":
            print(f"  [SOLVED] {guess.upper()} in {attempt + 1} attempt(s)")
            return True

        history.append((guess, result))
        next_guess = call_solver(history)
        if not next_guess:
            print("  [ERROR] Solver failed to produce a guess")
            return False

        guess = next_guess

    print(f"  [FAILED] Could not solve in {MAX_ATTEMPTS} attempts")
    return False


# ─── Main State Machine ──────────────────────────────────────────────────────

def main():
    if not os.path.exists(SOLVER_PATH):
        print(f"[ERROR] Solver not found at: {SOLVER_PATH}")
        print("Build it first: cargo build --release")
        sys.exit(1)

    print("=" * 55)
    print("  worfeory-cli | wordlecup.io solver")
    print("  Ctrl+C to stop")
    print("=" * 55)
    print(f"  solver : {SOLVER_PATH}")
    print(f"  opener : {FIRST_GUESS.upper()}")
    print()

    driver = create_driver()

    # Stats across all games
    stats = {"solved": 0, "failed": 0, "games": 0}
    current_game_round = None
    round_played_this_cycle = False
    last_state = None
    stuck_since = None  # timestamp when we entered an idle state

    try:
        print(f"[INIT] {WORDLE_URL}")
        driver.get(WORDLE_URL)
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "table.Game-rows") or
                      d.find_elements(By.XPATH, "//button[text()='Play Game']")
        )
        print("[INIT] Ready\n")

        # ─── Infinite state machine loop ──────────────────────────────────
        while True:
            state = detect_state(driver)

            # Only log state changes
            if state != last_state:
                print(f"\n[STATE] {state}")
                last_state = state
                # Reset stuck timer on any state change
                stuck_since = None

            # ── LOBBY: click "Play Game" to join ──────────────────────────
            if state == STATE_LOBBY:
                stuck_since = None
                print("  Joining game...")
                if click_play_game(driver):
                    print("  Joined.")
                    round_played_this_cycle = False
                    # Wait until board appears
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "table.Game-rows"))
                        )
                    except:
                        time.sleep(1)
                else:
                    print("  [WARN] Could not click Play Game")
                    time.sleep(1)

            # ── IN_GAME: play the round ───────────────────────────────────
            elif state == STATE_IN_GAME:
                stuck_since = None
                rnd, total = get_round_info(driver)
                rnd_str = f"Round {rnd}/{total}" if rnd else "Round ?/?"
                print(f"  {rnd_str}")

                solved = play_round(driver)

                if solved:
                    stats["solved"] += 1
                else:
                    stats["failed"] += 1

                round_played_this_cycle = True

                # After playing, check if it was last round
                if rnd and total and rnd >= total:
                    stats["games"] += 1
                    print(f"\n  [GAME {stats['games']} COMPLETE] solved={stats['solved']} failed={stats['failed']}")

                # Transition to WAITING or SCOREBOARD handled by state loop
                time.sleep(POLL_INTERVAL)

            # ── WAITING: solved/failed early, waiting for timer ───────────
            elif state == STATE_WAITING:
                if stuck_since is None:
                    stuck_since = time.time()
                time.sleep(POLL_INTERVAL)

            # ── SCOREBOARD: popup between rounds ─────────────────────────
            elif state == STATE_SCOREBOARD:
                stuck_since = None
                print("  Scoreboard visible, waiting for it to close...")
                # Wait for scoreboard to auto-dismiss
                while detect_state(driver) == STATE_SCOREBOARD:
                    time.sleep(0.5)
                print("  Scoreboard closed.")

                # Give the page time to transition (board reset / lobby)
                time.sleep(1.5)

                # Actively wait for a clear next state before resuming
                print("  Waiting for next round/game...")
                transition_start = time.time()
                while time.time() - transition_start < 30:
                    next_state = detect_state(driver)
                    if next_state in ACTIONABLE_STATES:
                        print(f"  Transitioned to {next_state}")
                        break
                    time.sleep(0.5)
                else:
                    # 30s passed without an actionable state — force refresh
                    print("  [WARN] Stuck after scoreboard, refreshing page...")
                    driver.get(WORDLE_URL)
                    time.sleep(3)

            # ── BETWEEN_GAMES: all rounds done, waiting for next game ────
            elif state == STATE_BETWEEN_GAMES:
                if stuck_since is None:
                    stuck_since = time.time()
                time.sleep(POLL_INTERVAL)

            # ── UNKNOWN: can't determine state ───────────────────────────
            elif state == STATE_UNKNOWN:
                if stuck_since is None:
                    stuck_since = time.time()
                time.sleep(POLL_INTERVAL)

            # ── Stuck too long in an idle state? Refresh. ─────────────────
            if stuck_since and (time.time() - stuck_since > 90):
                print("  [WARN] Stuck for 90s, refreshing page...")
                driver.get(WORDLE_URL)
                time.sleep(3)
                stuck_since = None
                last_state = None

    except KeyboardInterrupt:
        print("\n\n" + "=" * 55)
        print("  SESSION SUMMARY")
        print("=" * 55)
        total_rounds = stats['solved'] + stats['failed']
        print(f"  games    : {stats['games']}")
        print(f"  solved   : {stats['solved']}")
        print(f"  failed   : {stats['failed']}")
        print(f"  win rate : {stats['solved']/max(total_rounds,1)*100:.0f}%")
        print()
        print("Browser is still open. Press Enter to close.")
        input()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        print("\nBrowser is still open. Press Enter to close.")
        input()
    finally:
        driver.quit()
        print("Browser closed.")


if __name__ == "__main__":
    main()
