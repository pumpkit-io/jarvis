"""What the User reads aloud in the recording session, shared with the synthetic run.

Clip ids are stable: `make_synthetic.py` and `session.py` write `<id>.wav` under
their own folder, so every benchmark runs unchanged on synthetic or recorded audio.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Clip:
    id: str
    lang: str  # "en" or "it"
    kind: str  # "short" (about 2 s) or "long" (about 6 s)
    text: str


CLIPS: list[Clip] = [
    Clip("en01", "en", "short", "What are you working on?"),
    Clip("en02", "en", "short", "Open a PR for the typo fix."),
    Clip("en03", "en", "short", "Cancel that."),
    Clip("en04", "en", "short", "Is the CI green on pumpkit?"),
    Clip("en05", "en", "short", "Merge pull request forty two."),
    Clip("en06", "en", "short", "Wait, stop."),
    Clip("en07", "en", "short", "Thanks Jarvis, that's all."),
    Clip("en08", "en", "short", "Turn on quiet mode."),
    Clip("en09", "en", "short", "Show me the open issues."),
    Clip("en10", "en", "short", "How many users signed up this week?"),
    Clip("en11", "en", "long", "Open an issue in pumpkit about the scheduler skipping posts when the timezone changes, and assign it to me."),
    Clip("en12", "en", "long", "Ask a subagent to review the open pull request on motion studio and tell me if anything blocks the merge."),
    Clip("en13", "en", "long", "Actually, instead of a new branch, add the fix to the existing pull request and rerun the tests on CI."),
    Clip("en14", "en", "long", "When the deploy finishes, check the error rate in PostHog and let me know if it went up compared with yesterday."),
    Clip("en15", "en", "long", "Write a short summary of what changed in the sdlc skills this week and put the details on screen."),
    Clip("it01", "it", "short", "Su cosa stai lavorando?"),
    Clip("it02", "it", "short", "Apri una PR per la correzione."),
    Clip("it03", "it", "short", "Annulla."),
    Clip("it04", "it", "short", "La CI è verde su pumpkit?"),
    Clip("it05", "it", "short", "Aspetta, un attimo."),
    Clip("it06", "it", "short", "Grazie Jarvis, basta così."),
    Clip("it07", "it", "short", "Attiva la modalità silenziosa."),
    Clip("it08", "it", "short", "Mostrami le issue aperte."),
    Clip("it09", "it", "short", "Quanti utenti si sono iscritti oggi?"),
    Clip("it10", "it", "short", "Sì, procedi pure."),
    Clip("it11", "it", "long", "Apri una issue su pumpkit per il problema dei post programmati che saltano quando cambia il fuso orario."),
    Clip("it12", "it", "long", "Chiedi a un subagente di rivedere la pull request aperta su motion studio e dimmi se qualcosa blocca il merge."),
    Clip("it13", "it", "long", "Anzi, invece di un nuovo branch, aggiungi la correzione alla pull request esistente e rilancia i test."),
    Clip("it14", "it", "long", "Quando finisce il deploy, controlla il tasso di errori su PostHog e dimmi se è salito rispetto a ieri."),
    Clip("it15", "it", "long", "Scrivi un breve riassunto di cosa è cambiato nelle skill sdlc questa settimana e mostrami i dettagli."),
]

# Each phrasing is recorded TAKES_PER_PHRASING times in the session.
WAKE_PHRASINGS: dict[str, str] = {
    "hey": "Hey Jarvis",
    "hi": "Hi Jarvis",
    "ehi": "Ehi Jarvis",
    "bare": "Jarvis",
}
TAKES_PER_PHRASING = 10

# What the Hub would say during the speaker run, so the echo is Jarvis's own voice.
JARVIS_LINES: list[tuple[str, str]] = [
    ("en", "I've opened the pull request on pumpkit, sir, and the checks are running now."),
    ("en", "The deploy finished twelve minutes ago, and the error rate is flat compared with yesterday."),
    ("en", "Two subagents are still working: one on the scheduler bug, one on the motion studio review."),
    ("it", "Ho aperto la pull request su pumpkit, signore, e i controlli sono in corso."),
    ("it", "Il deploy è terminato dodici minuti fa e il tasso di errori è stabile rispetto a ieri."),
]

TTS_SENTENCES: dict[str, str] = {
    "en": "I have opened the pull request on pumpkit and checks are running.",
    "it": "Ho aperto la pull request su pumpkit e i controlli sono partiti.",
}

PAUSE = " … "


@dataclass(frozen=True)
class PauseTake:
    id: str
    lang: str
    text: str  # PAUSE marks where the User stops for about a second before finishing
    paused: bool


# Real mid-sentence pauses for Smart Turn: a cut at the pause must read as incomplete.
PAUSE_TAKES: list[PauseTake] = [
    PauseTake("p_en01", "en", f"Open an issue in pumpkit{PAUSE}about the scheduler skipping posts.", True),
    PauseTake("p_en02", "en", f"Ask a subagent to{PAUSE}review the motion studio pull request.", True),
    PauseTake("p_en03", "en", f"When the deploy finishes,{PAUSE}check the error rate in PostHog.", True),
    PauseTake("p_en04", "en", f"Write a summary of{PAUSE}what changed in the sdlc skills this week.", True),
    PauseTake("p_it01", "it", f"Apri una issue su pumpkit{PAUSE}per i post che saltano.", True),
    PauseTake("p_it02", "it", f"Chiedi a un subagente di{PAUSE}rivedere la pull request su motion studio.", True),
    PauseTake("p_it03", "it", f"Quando finisce il deploy,{PAUSE}controlla il tasso di errori.", True),
    PauseTake("p_it04", "it", f"Scrivi un riassunto di{PAUSE}cosa è cambiato questa settimana.", True),
    PauseTake("c_en01", "en", "Hmm, let me think about that and get back to you.", False),
    PauseTake("c_en02", "en", "Put the list of open pull requests on screen.", False),
    PauseTake("c_it01", "it", "Fammi pensare, poi ti dico.", False),
    PauseTake("c_it02", "it", "Mostrami le pull request aperte.", False),
]
