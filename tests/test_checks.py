# SPDX-License-Identifier: Apache-2.0
import pytest

from wavescribe.asr.checks import Check, check_transcript


def test_drops_more_text_than_the_audio_can_hold() -> None:
    assert check_transcript("palavra " * 30, 2.0, "pt-PT").drop == "too_long"
    assert check_transcript("O governo aprovou hoje o orçamento.", 2.5, "pt-PT").drop is None


# What a decoder invents over music or noise: the loop, and next to nothing else.
LOOPS = [
    ("Segundo, saco, frago, frago, frago, frago, frago.", 6, "pt-PT"),
    ("that is it, that is it, that is it, that is it", 6, "en"),
    ("так, так, так, так, так, так", 4, "uk"),
    ("Evacuate, evacuate, evacuate, evacuate, evacuate.", 5, "en"),
    # Japanese has no spaces between words: the compression check catches the loop.
    ("ありがとうございます" * 8, 20, "ja"),
    ("ขอบคุณที่รับชม " * 8, 20, "th"),
    # However it starts, nobody says the same thing twenty times in a row.
    ("The minister said today. " + "Thank you. " * 20, 20, "en"),
]

# How emergencies are announced: a call repeated for emphasis, then the message.
EMPHASIS = [
    ("Pan-pan, pan-pan, pan-pan, all stations, this is sailing vessel Aurora.", 6, "en"),
    ("Atenção, atenção, atenção, atenção. Evacuem a zona ribeirinha.", 6, "pt-PT"),
    ("Evacuate now. Evacuate now. Evacuate now. Evacuate now. Go to higher ground.", 8, "en"),
    ("注意、注意、注意、注意。川沿いの地域から避難してください。", 8, "ja"),
]


@pytest.mark.parametrize(("text", "seconds", "language"), LOOPS)
def test_drops_repetition_loops_in_any_script(text: str, seconds: float, language: str) -> None:
    assert check_transcript(text, seconds, language).drop == "repetition"


@pytest.mark.parametrize(("text", "seconds", "language"), EMPHASIS)
def test_keeps_and_flags_a_repeated_call_with_a_message(
    text: str, seconds: float, language: str
) -> None:
    check = check_transcript(text, seconds, language)
    assert check.drop is None and check.flags == ("repetition",)


def test_ordinary_repeats_are_neither_dropped_nor_flagged() -> None:
    ok = "Não, não, não. O ministro disse que a decisão será conhecida amanhã de manhã."
    assert check_transcript(ok, 6, "pt-PT") == Check()
    assert check_transcript("Costa, costa.", 1.4, "pt-PT") == Check()
    mayday = "Mayday, mayday, mayday, this is fishing vessel Maria. We are taking on water."
    assert check_transcript(mayday, 7, "en") == Check()


# A weather bulletin. Thai letters take three bytes each in UTF-8, which once made ordinary
# text look as repetitive as a loop: the measure is per character.
THAI = (
    "กรมอุตุนิยมวิทยาประกาศเตือนพายุฝนฟ้าคะนองและลมกระโชกแรงในหลายจังหวัดทางภาคเหนือ "
    "ขอให้ประชาชนระมัดระวังอันตรายจากน้ำท่วมฉับพลันและน้ำป่าไหลหลาก "
    "โดยเฉพาะพื้นที่เชิงเขาและที่ราบลุ่มใกล้ทางน้ำไหลผ่าน "
    "ประชาชนในพื้นที่ริมแม่น้ำให้ติดตามประกาศจากทางราชการอย่างใกล้ชิด "
    "และเตรียมพร้อมอพยพไปยังศูนย์พักพิงที่ใกล้ที่สุดหากระดับน้ำสูงขึ้น"
)
HINDI = (
    "मौसम विभाग ने कई जिलों में भारी बारिश की चेतावनी जारी की है। नदी किनारे रहने वाले लोगों "
    "से ऊंचे स्थानों पर जाने और प्रशासन के निर्देशों का पालन करने को कहा गया है।"
)


@pytest.mark.parametrize(("text", "seconds", "language"), [(THAI, 25, "th"), (HINDI, 12, "hi")])
def test_ordinary_speech_in_scripts_with_long_letters_is_not_a_loop(
    text: str, seconds: float, language: str
) -> None:
    assert check_transcript(text, seconds, language).drop is None


def test_flags_text_clearly_in_another_language() -> None:
    spanish = "No quiero saber nada de lo que pasó ayer en la ciudad."
    assert check_transcript(spanish, 4, "pt-PT").flags == ("lang:es",)
    english = "Some of the venues were full of people who had never heard it before."
    assert check_transcript(english, 5, "pt-PT").flags == ("lang:en",)
    portuguese = "Corte de água amanhã de manhã em vários bairros da cidade."
    assert check_transcript(portuguese, 4, "pt-PT").flags == ()
    assert check_transcript(english, 5, "en-GB").flags == ()
    ukrainian = "Завтра вранці не буде води в кількох районах міста."
    assert check_transcript(ukrainian, 4, "uk").flags == ()


def test_no_language_verdict_on_short_text_or_unknown_languages() -> None:
    assert check_transcript("Foi preso.", 1, "pt-PT").flags == ()  # reads as Romanian
    assert check_transcript("No quiero saber nada de lo que pasó.", 3, "tok").flags == ()


# What models write over silence and music, in several languages, as the ASR returns it.
JUNK = [
    ("Thanks for watching!", "en-GB"),
    ("Thank you for watching. Please subscribe to my channel.", "en"),
    ("Legendas pela comunidade Amara.org", "pt-PT"),
    ("Subtítulos realizados por la comunidad de Amara.org", "es-ES"),
    ("Untertitel im Auftrag des ZDF, 2017", "de-DE"),
    ("Untertitelung des ZDF für funk, 2020", "pl"),  # whatever the station's language
    ("Sous-titrage ST' 501", "fr-FR"),
    ("Sottotitoli creati dalla comunità Amara.org", "it"),
    ("Napisy stworzone przez społeczność Amara.org", "pl"),
    ("Субтитры сделал Иван Петров", "ru"),
    ("Редактор субтитров О.Петрова Корректор Н.Иванова", "uk"),  # noqa: RUF001
    ("Дякую за перегляд!", "uk"),
    ("İzlediğiniz için teşekkürler.", "tr"),
    ("شكراً على المشاهدة", "ar"),
    ("ご視聴ありがとうございました", "ja"),
    ("시청해주셔서 감사합니다.", "ko"),
    ("字幕由Amara.org社区提供", "zh"),
    ("ขอบคุณที่รับชม", "th"),
    ("Merci d'avoir regardé cette vidéo ! Abonnez-vous.", "fr-CA"),
]


@pytest.mark.parametrize(("text", "language"), JUNK)
def test_drops_subtitle_credits_and_sign_offs_in_any_language(text: str, language: str) -> None:
    assert check_transcript(text, 3, language).drop == "junk"


# The same words where someone is really speaking: anything left over keeps the whole line.
NOT_JUNK = [
    ("Subscribe to our podcast for the full interview.", "en"),
    ("A proteção civil pede às populações que saiam. Obrigado por assistir.", "pt-PT"),
    ("Obrigado por nos ouvir.", "pt-PT"),  # radio's own sign-off
    ("Thank you.", "en"),
    ("Gracias por ver la final con nosotros esta noche en la radio.", "es"),
    ("Die Untertitel des Films waren schlecht übersetzt worden.", "de"),
]


@pytest.mark.parametrize(("text", "language"), NOT_JUNK)
def test_keeps_lines_that_say_more_than_a_sign_off(text: str, language: str) -> None:
    assert check_transcript(text, 4, language).drop is None


def test_every_listed_phrase_is_junk_on_its_own() -> None:
    import tomllib
    from importlib import resources

    from wavescribe.asr.checks import is_junk

    raw = tomllib.loads(
        resources.files("wavescribe.asr").joinpath("junk.toml").read_text(encoding="utf-8")
    )
    phrases = [p for section in raw.values() for p in section.get("phrases", ())]
    assert len(phrases) > 40 and len(raw) > 10  # many languages, not one
    for phrase in phrases:
        assert is_junk(phrase), phrase
        assert is_junk(f"{phrase.upper()}!!"), phrase  # case and punctuation don't matter
        assert not is_junk(f"{phrase} — and then the minister said no"), phrase
