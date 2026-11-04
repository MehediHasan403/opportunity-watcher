# Opportunity Watcher

Runs free on GitHub **every 4 hours**. It checks the **SALTO-YOUTH European Training Calendar**, 19 web searches and 8 university/event pages. When something **new** matches your requirements, it **emails you**.

| What it watches | What counts as a match |
|---|---|
| **SALTO European Training Calendar.** Every upcoming training is opened and read. | It must be open to people living in Germany, include one of your topics (digital, AI, ICT, coding, robotics, IoT, embedded, C++, employability…), and its deadline must not have passed. |
| **19 web searches** via Google News: BIPs in embedded/IoT/robotics, Rhine-Waal Erasmus, embedded world Student Day, C++ workshops and meetups, hackathons, digital youth-work trainings. | Any new result from the last 30 days. |
| **University and event pages:** ISEC Coimbra BIP, Wrocław KCiR, Euclides Network, Rhine-Waal news, embedded world Student Day, Meeting C++, JUGEND für Europa. | New links with your keywords, or new text on the page. |

Every SALTO match in the email shows the dates, place, **deadline with days left**, who can apply, and what's paid (✅ no fee / ✅ accommodation / ✅ travel up to X €). It also tells you when you need to ask JUGEND für Europa first.

---

## Setup (about 15 minutes, once)

The code is already in this repository. You only need to connect your email.

### 1. Create a Gmail App Password
1. Go to **myaccount.google.com → Security** and turn on **2-Step Verification**.
2. Open **myaccount.google.com/apppasswords**, name it `Opportunity watcher` and click **Create**.
3. Copy the 16-letter password.

### 2. Add 3 secrets to this repository
**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | the 16-letter App Password |
| `EMAIL_TO` | where alerts should go (can be the same Gmail) |

Secrets are encrypted. Nobody, including you, can read them back, even though the repository is public.

### 3. Let the watcher save its memory
**Settings → Actions → General → Workflow permissions →** select **Read and write permissions → Save**.

### 4. Test the email
**Actions** tab → **Opportunity watcher** → **Run workflow** → mode **test-email** → **Run workflow**.
You should get an email within a minute. Check spam the first time and mark it **Not spam**.

### 5. Start it
**Run workflow** again with mode **normal**.
- **First email:** *"Opportunity watcher is live: N SALTO training(s) match you now"*. That's every SALTO training that matches you today.
- **After that:** it runs automatically every 4 hours and emails you only when something new appears.

Optional: mode **dry-run** checks everything and shows the results in the run log (Actions → the run → *Run watcher*) without sending email.

---

## Changing your requirements

Edit **`config.yaml`** on GitHub (open it → pencil icon → **Commit changes**). The next run uses the new settings.

- **SALTO topics:** `salto: topics:`. Add or remove words.
- **Only certain types:** `salto: activity_types: [Training Course, Seminar]`.
- **Skip things:** `salto: exclude_words: [youth exchange]`.
- **Add a web search:** add a line under `web_search: terms:`. Quotes mean an exact phrase, `OR` means either word.
- **Watch another page:** add a block under `sources:` (examples are in the file).
- **Pause something:** add `enabled: false` under it.
- **How often:** edit `cron:` in `.github/workflows/watch.yml`. Times are UTC. `"17 */2 * * *"` means every 2 hours.

## How it works

| File | Purpose |
|---|---|
| `salto.py` | Reads SALTO's training list (all pages), opens each new training and extracts its fields. Checks them against your requirements and tags the costs. It reads the page text rather than CSS classes, so small site redesigns don't break it. |
| `watcher.py` | Runs the SALTO scan, web searches, feeds and pages, sends the email, and remembers what you've seen. |
| `config.yaml` | Your requirements, search terms and sources. |
| `state.json` | Created automatically. Stores the IDs of items already checked, so nothing is emailed twice. |
| `tests/` | Offline tests with sample SALTO pages. They run on every push (*Tests* workflow). |

**Good to know**
- The first time a web search or page is checked, its existing results are only remembered, not emailed. Old news isn't new. SALTO is the exception: its current matches come in the first email.
- If a source keeps failing (about a day in a row), the next email tells you so. If a whole run crashes, GitHub emails you.
- A public repository has unlimited free Actions minutes. Anyone can see `config.yaml` (your interests), but never your secrets.
- It can't read Instagram or LinkedIn. Turn on notifications there for Rhine-Waal's International Office.

## Run it on your own computer (optional)

```bash
pip install -r requirements.txt
python watcher.py --dry-run          # just print what it finds
python -m pytest -q                  # run the tests
```
