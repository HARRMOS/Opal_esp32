"""Lecteur du haut-parleur : pause pour la voix, reprise, volume, arrêt."""

import asyncio

import numpy as np

import player


def run(coro):
    return asyncio.run(coro)


def fake_decoder(seconds: float):
    """Remplace le téléchargement : `seconds` de son constant, décodé tout de suite."""
    def decode(url, stop, emit):
        t = np.arange(player.CHUNK // 2) / player.RATE
        tone = (np.sin(2 * np.pi * 1500 * t) * 8000).astype("<i2").tobytes()  # 1,5 kHz : pas filtré
        for _ in range(int(seconds * player.RATE * 2 / player.CHUNK)):
            if stop.is_set():
                break
            emit(tone)
        emit(None)
    return decode


def make(monkeypatch, seconds=2.0):
    monkeypatch.setattr(player, "decode_stream", fake_decoder(seconds))
    monkeypatch.setattr(player, "LEAD_S", 0.2)
    sent = {"json": [], "pcm": []}

    async def send_json(m):
        sent["json"].append(m["type"] + (":" + m["value"] if "value" in m else ""))
        return 1

    async def send_pcm(b):
        sent["pcm"].append(b)
        return 1

    return player.Player(send_json, send_pcm), sent


def test_pause_resume_and_volume(monkeypatch):
    async def go():
        p, sent = make(monkeypatch)
        await p.play("https://x/a.mp3", "Al-Mulk")
        await asyncio.sleep(0.4)
        assert await p.pause(for_voice=True) and p.paused and p.playing
        assert sent["json"][-1] == "audio_end"  # le boîtier peut recevoir la voix d'Opal
        n = len(sent["pcm"])
        await asyncio.sleep(0.4)
        assert len(sent["pcm"]) <= n + 1  # plus rien ne part pendant la pause
        p.set_volume(35)
        assert await p.resume()
        await asyncio.sleep(0.3)
        assert "audio_start" in sent["json"][-2:]
        last = np.frombuffer(sent["pcm"][-1], dtype="<i2").astype(float)
        assert abs(np.abs(last).max() - 4000) < 120  # 35 % au lieu de 70 % : moitié
        assert p.status()["volume"] == 35 and not p.status()["paused"]
        assert await p.stop() and not p.playing
        assert sent["json"][-2:] == ["audio_end", "state:idle"]

    run(go())


def test_speaker_filter_cuts_bass_and_rounds_peaks():
    def rms(hz, gain=1.0):
        f = player.SpeakerFilter()
        t = np.arange(player.RATE) / player.RATE
        x = (np.sin(2 * np.pi * hz * t) * 16000).astype("<i2").tobytes()
        out = b"".join(f.process(x[i:i + 4096], gain) for i in range(0, len(x), 4096))
        y = np.frombuffer(out, dtype="<i2")[player.RATE // 2:].astype(float)
        return np.sqrt((y ** 2).mean()) / (16000 / np.sqrt(2)), np.abs(y).max()

    assert rms(60)[0] < 0.1          # graves que le haut-parleur ne rend pas : coupés
    assert rms(1000)[0] > 0.97       # voix : intacte
    assert rms(10000)[0] > 0.97      # aigus : intacts (48 kHz)
    assert rms(1000, gain=2.5)[1] < 32767  # trop fort : arrondi, jamais coupé net


def test_stop_while_paused(monkeypatch):
    async def go():
        p, _ = make(monkeypatch)
        await p.play("https://x/a.mp3")
        await asyncio.sleep(0.2)
        await p.pause()
        assert await asyncio.wait_for(p.stop(), 2)

    run(go())
