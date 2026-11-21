"""PRS league bot: reads public channels, proposes website updates, publishes them after a staff tick."""
import os, re, json, base64, html, datetime, logging
import aiohttp, discord
from aiohttp import web
from anthropic import AsyncAnthropic

SECRET = re.compile(r"(sk-ant-[\w-]+|github_pat_\w+|ghp_\w+|[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}|Bearer\s+\S+)")
def redact(t):
    return SECRET.sub("[hidden]", str(t))
_format = logging.Formatter.format
logging.Formatter.format = lambda self, rec: redact(_format(self, rec))   # keys never appear in logs
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("prs")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"].strip()
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"].strip()
REPO = os.environ["GITHUB_REPO"].strip()                      # e.g. PRS-art/PRS-Racing
APPROVAL_ID = int(os.environ["APPROVAL_CHANNEL_ID"])
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_IMAGES = 4

def id_set(name):
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}
SKIP_CHANNELS, SKIP_CATEGORIES = id_set("SKIP_CHANNEL_IDS"), id_set("SKIP_CATEGORY_IDS")
SKIP_WORDS = [w for w in os.getenv("SKIP_NAME_WORDS", "staff,private,ticket,admin").lower().split(",") if w]
KEYWORDS = re.compile(r"standing|result|signing|signed|lineup|line-up|announce|penalt|calendar|championship|"
                      r"points|transfer|released|promot|principal|\bTP\b|welcome|new season|round \d", re.I)

claude = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())   # strip() removes stray spaces or line breaks from pasted keys
pending = {}                                          # approval message id -> proposal

SYSTEM = """You read messages from a sim-racing league Discord (PEAK Racing Series, "PRS"). Tiers: "t1" (Tier 1), "t2" (Tier 2), "f1" (F1 25 tier).
Decide whether the message contains something to publish on the league website. Reply with ONE JSON object and nothing else:
{"kind":"none|standings|occ|lineup|news","tier":"t1|t2|f1|null","summary":"one short sentence","data":{...}}
- none: chat, banter, questions, anything unclear. If unsure, use none.
- standings (tier required): data={"drivers":[[name,team,points],...],"constructors":[[team,points],...],"next_round":int|null}. Include only the tables shown. Copy numbers exactly.
- occ (overall constructors table across tiers): data={"occ":[[team,points],...]}
- lineup (signing, release or team principal change; tier required): data={"team":"team name","seat":"driver1|driver2|tp","driver":[name,number|null,tag|null] or null if the seat is now empty}
- news (announcements, race results, rule changes, anything else worth posting): data={"title":"short headline","text":"1-3 sentences"}
Use plain usernames without flags, team names or | separators. Never invent anything that is not in the message or images."""

def watched(ch):
    """Public channels only: anything @everyone cannot see (staff, team channels) is skipped."""
    try:
        cat = getattr(ch, "category", None)
        if ch.id == APPROVAL_ID or ch.id in SKIP_CHANNELS or (cat and cat.id in SKIP_CATEGORIES):
            return False
        name = f"{ch.name} {cat.name if cat else ''}".lower()
        if any(w in name for w in SKIP_WORDS):
            return False
        return bool(ch.permissions_for(ch.guild.default_role).view_channel)
    except Exception:
        return False

def clean(x):
    if isinstance(x, str):
        return html.escape(re.sub(r"\s+", " ", x).strip()[:200])
    if isinstance(x, list):
        return [clean(i) for i in x]
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    return x

async def classify(msg):
    content = []
    async with aiohttp.ClientSession() as s:
        for a in [a for a in msg.attachments if (a.content_type or "").split(";")[0] in
                  ("image/png", "image/jpeg", "image/gif", "image/webp")][:MAX_IMAGES]:
            if a.size > 5_000_000:
                continue
            async with s.get(a.url) as r:
                raw = await r.read()
            content.append({"type": "image", "source": {"type": "base64",
                            "media_type": a.content_type.split(";")[0], "data": base64.b64encode(raw).decode()}})
    content.append({"type": "text", "text": f"Channel: #{msg.channel.name}\nAuthor: {msg.author.display_name}\n"
                                            f"Message:\n{msg.clean_content[:3000] or '(no text)'}"})
    r = await claude.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM,
                                     messages=[{"role": "user", "content": content}])
    text = re.sub(r"^```(?:json)?|```$", "", r.content[0].text.strip(), flags=re.M).strip()
    return json.loads(text)

def find_team(rows, name):
    n = name.lower()
    for i, row in enumerate(rows):
        t = row[0].lower()
        if t in n or n in t:
            return i
    raise ValueError(f"unknown team: {name}")

def apply(data, p):
    kind, d, tier = p["kind"], p["data"], p.get("tier")
    if kind in ("standings", "lineup") and tier not in data["tiers"]:
        raise ValueError(f"unknown tier: {tier}")
    if kind == "standings":
        t = data["tiers"][tier]
        if d.get("drivers"):
            t["drivers"] = sorted(([n, tm, int(pt)] for n, tm, pt in d["drivers"]), key=lambda r: -r[2])
        if d.get("constructors"):
            t["constructors"] = sorted(([tm, int(pt)] for tm, pt in d["constructors"]), key=lambda r: -r[1])
        if d.get("next_round") is not None:
            t["current"] = int(d["next_round"])
    elif kind == "occ":
        old = {r[0].lower(): r for r in data["occ"]}
        out = []
        for tm, pt in sorted(d["occ"], key=lambda r: -int(r[1])):
            o = old.get(tm.lower())
            out.append([o[0] if o else tm, int(pt)] + (o[2:] if o else ["#2a2f3d"]))
        data["occ"] = out
    elif kind == "lineup":
        rows = data["lineups"][tier]
        row = rows[find_team(rows, d["team"])]
        drv = d.get("driver")
        drv = [drv[0], drv[1] if len(drv) > 1 else None, drv[2] if len(drv) > 2 else None] if drv else None
        if d["seat"] == "tp":
            while len(row) < 3:
                row.append(None)
            row[2] = drv
        elif d["seat"] in ("driver1", "driver2"):
            row[1][0 if d["seat"] == "driver1" else 1] = drv
        else:
            raise ValueError("unknown seat")
    elif kind == "news":
        data.setdefault("news", []).insert(0, {"date": datetime.date.today().strftime("%-d %b %Y"),
                                               "title": d["title"], "text": d["text"]})
        del data["news"][12:]
    else:
        raise ValueError(f"unknown kind: {kind}")

API = f"https://api.github.com/repos/{REPO}/contents/data.json"
GH = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"}

async def publish(p, who):
    async with aiohttp.ClientSession(headers=GH) as s:
        async with s.get(API) as r:
            r.raise_for_status()
            j = await r.json()
        data = json.loads(base64.b64decode(j["content"]))
        apply(data, p)
        data["updated"] = datetime.date.today().isoformat()
        body = {"message": f"{p['kind']}: {p['summary'][:60]} (approved by {who})", "sha": j["sha"],
                "content": base64.b64encode(json.dumps(data, ensure_ascii=False, indent=1).encode()).decode()}
        async with s.put(API, json=body) as r:
            r.raise_for_status()

class Bot(discord.Client):
    async def setup_hook(self):                       # tiny web page so the host sees the service as healthy
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8000"))).start()

    async def note(self, text):
        try:
            ch = self.get_channel(APPROVAL_ID) or await self.fetch_channel(APPROVAL_ID)
            await ch.send(redact(text)[:1900])
        except Exception:
            log.exception("cannot post to the approvals channel (check APPROVAL_CHANNEL_ID and the bot's permissions there)")

    async def on_ready(self):
        log.info("online as %s", self.user)
        if not getattr(self, "said_hello", False):
            self.said_hello = True
            await self.note("✅ PRS bot is online and can post here. To test it, write !prs followed by any message in a public channel, and I'll say what I did with it.")

    async def on_message(self, m):
        if m.author.bot or not m.guild:
            return
        debug = m.content.lower().startswith("!prs")
        if not watched(m.channel):
            if debug:
                await self.note(f"ℹ️ Skipped #{m.channel}: the bot treats this channel as private or ignored.")
            return
        if not (debug or m.attachments or KEYWORDS.search(m.content)):
            return
        try:
            p = await classify(m)
            if p.get("kind") in (None, "none"):
                if debug:
                    await self.note("ℹ️ Claude read it and decided it isn't something to publish: " + str(p.get("summary") or "no reason given"))
                return
            p["summary"] = str(p.get("summary") or "")[:200]
            p["data"] = clean(p.get("data") or {})
            emb = discord.Embed(title=f"{p['kind']} · {p.get('tier') or 'all'}", color=0x3B6CFF,
                                description=p["summary"] + "\n\n```json\n" + json.dumps(p["data"], ensure_ascii=False)[:1500] + "\n```")
            emb.add_field(name="Source", value=m.jump_url)
            ch = self.get_channel(APPROVAL_ID) or await self.fetch_channel(APPROVAL_ID)
            msg = await ch.send(embed=emb)
            for e in ("✅", "❌"):
                await msg.add_reaction(e)
            pending[msg.id] = p
        except Exception as ex:
            log.exception("could not process message %s", m.id)
            await self.note(f"⚠️ Could not process a message: {type(ex).__name__}: {str(ex)[:300]}")

    async def on_raw_reaction_add(self, ev):
        if ev.channel_id != APPROVAL_ID or ev.user_id == self.user.id or ev.message_id not in pending:
            return
        e, who = str(ev.emoji), (ev.member.display_name if ev.member else str(ev.user_id))
        if e not in ("✅", "❌"):
            return
        p = pending.pop(ev.message_id)
        if e == "✅":
            try:
                await publish(p, who)
                note = f"✅ Published by {who}"
            except Exception as ex:
                log.exception("publish failed")
                note = f"⚠️ Publish failed: {ex}"
        else:
            note = f"❌ Rejected by {who}"
        msg = await self.get_channel(APPROVAL_ID).fetch_message(ev.message_id)
        emb = msg.embeds[0]
        emb.set_footer(text=note)
        await msg.edit(embed=emb)

intents = discord.Intents.default()
intents.message_content = True
Bot(intents=intents).run(DISCORD_TOKEN)
