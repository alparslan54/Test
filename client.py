from ctypes import c_wchar
from fractions import Fraction


import customtkinter as ctk
from customtkinter import CTkImage
import websockets
from PIL import Image, ImageTk
from aiortc.sdp import candidate_from_sdp
import cv2

from av import VideoFrame
from aiortc.mediastreams import MediaStreamTrack
import json
import io
import time
import threading
import sys
import ssl
import traceback
import tkinter

from jinja2.ext import with_

from crypto_e2ee import pubkey_from_bytes, derive_aes_key

import winsound
from aiortc import RTCConfiguration, RTCIceServer
import pydub # new
import os    # new
import base64 # new
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame
import asyncio   #
import sounddevice as sd  #
import numpy as np

import sys
import os

def resource_path(relative_path):
    """ .exe olarak çalışırken kaynak dosyalarına doğru yolu alır """
    try:
        # PyInstaller geçici bir klasör oluşturur ve yolu _MEIPASS içinde saklar
        base_path = sys._MEIPASS

        # ---- YENİ SATIR ----
        # PyInstaller'ın 'data' dosyalarını (ffmpeg vb.) koyduğu
        # _internal klasörünü de yola ekle.
        base_path = os.path.join(base_path, ".")
        # ---- YENİ SATIR SONU ----

    except Exception:
        # .exe olarak çalışmıyorsa (normal .py ise)
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

if sys.stdin is None:
    sys.stdin = io.StringIO()
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# --- new block--- to show pydub ffmpeg's place
try:

    ffmpeg_path = resource_path("ffmpeg.exe")
    ffprobe_path = resource_path("ffprobe.exe")

    # hardcore these paths to pydub lib
    pydub.AudioSegment.converter = ffmpeg_path
    pydub.AudioSegment.ffprobe = ffprobe_path

    print("DEBUG: ffmpeg motoru pydub'a başarıyla bağlandı.")
except Exception as e:
    print(f"UYARI: ffmpeg/ffprobe yüklenemedi. Ses sıkıştırma çalışmayabilir. Hata: {e}")





ctk.set_appearance_mode("dark")

ctk.set_default_color_theme("blue")



#sound catcher class

class SoundDeviceAudioTrack(MediaStreamTrack):
    """
    A custom audio stream (track) class using 'sounddevice' for aiortc. (Start signal added)
    """
    kind = "audio"

    def __init__(self, loop, samplerate=48000, channels=1):
        super().__init__()
        self.loop = loop
        if samplerate is None:
            try:
                samplerate = int(sd.query_devices(kind='input')['default_samplerate'])
            except Exception:
                samplerate = 44100

        self.samplerate = samplerate
        self.channels = channels
        self.dtype = 'int16'  # s16
        self.blocksize = 1024  # 1024 sample

        self.stream = None
        self.thread = None
        self.queue = asyncio.Queue()
        self._running = True

        # --- YENİ SATIR ---
        self.started_event = asyncio.Event()  # Hazır olduğunda sinyal vermek için

        print("DEBUG (MicTrack): sound catcher (SoundDeviceAudioTrack) has started")
    def start_stream(self):

        def audio_callback(indata, frames, time, status):
            """(Runs in Thread) Called when sound is received from sounddevice."""
            if not self._running:
                raise sd.CallbackStop  # stop the thread

            if indata.shape[1] > 1:
                indata = np.mean(indata, axis=1, keepdims=True).astype(self.dtype)

            self.loop.call_soon_threadsafe(self.queue.put_nowait, indata.copy())

        try:
            self.stream = sd.InputStream(
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                dtype=self.dtype,
                channels=self.channels,
                callback=audio_callback
            )
            self.stream.start()




            # Safely send Initialization completed signal to main asyncio loop
            self.loop.call_soon_threadsafe(self.started_event.set)
            print("DEBUG (MicTrack): Microfon(sounddevice) stream started")

        except Exception as e:
            print(f"HATA (MicTrack): sounddevice InputStream couldnt start: {e}")
            self._running = False
            # In case of error, set 'Event' so that the 'await' one doesn't get stuck
            self.loop.call_soon_threadsafe(self.started_event.set)





    async def recv(self):
        """Called by aiortc. Retrieves an audio frame from the queue."""
        if not self._running:
            raise asyncio.CancelledError

        indata = await self.queue.get()
        frame = AudioFrame.from_ndarray(indata, format='s16', layout='mono')
        frame.sample_rate = self.samplerate

        if not hasattr(self, "timestamp"):
            self.timestamp = 0
        self.timestamp += len(indata)
        frame.pts = self.timestamp
        frame.time_base = Fraction(1 , self.samplerate)

        return frame


    async def start(self):
        """This is called when the track is added and WAITS for the stream to start."""
        if self.thread is None:
            print("DEBUG (MicTrack): start thread has been creating")
            self._running = True
            self.started_event.clear()  # Event'i sıfırla
            self.thread = threading.Thread(target=self.start_stream, daemon=True)
            self.thread.start()

            # Wait here until the microphone stream calls 'self.started_event.set()'
            await self.started_event.wait()
            print("DEBUG (MicTrack): Stream 'started' signal received.")

    def stop(self):
        """Called when this track is stopped."""
        if self._running:
            print("DEBUG (MicTrack): Mikrofon (sounddevice) stream stopping..")
            self._running = False
            if self.thread:
                self.thread.join(timeout=1)
                self.thread = None
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            print("DEBUG (MicTrack): Mikrofon stopped.")


# --- NEW WebRTCManager CLASS (SoundDevice Integrated) ---
# (Added initialization delay fix)

class DummyVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, loop, color=(0, 255, 0)):
        super().__init__()
        self.loop = loop
        self.color = color  # RGB renk (default yeşil)
        self._running = True

    async def recv(self):
        # 320x240 sabit renkli kare üret
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:] = self.color
        frame = VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts = 0
        frame.time_base = Fraction(1,30)  # 30 FPS
        return frame

    def stop(self):
        self._running = False


class CameraVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, loop, camera_index=0):
        super().__init__()
        self.loop = loop
        self.cap = cv2.VideoCapture(camera_index)
        print("Camera opened", self.cap.isOpened())
        self._running = True
        self.queue = asyncio.Queue()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        while self._running:
            ret, frame = self.cap.read()
            if not ret:
                continue

            # 1. O anki zamanı saniye olarak al
            now_seconds = time.time()

            # OpenCV -> VideoFrame
            video_frame = VideoFrame.from_ndarray(frame, format="bgr24")

            # 2. Zaman birimini (time_base) ayarla (milisaniye)
            video_frame.time_base = Fraction(1 , 1000)

            # 3. Zaman damgasını (pts) ayarla (milisaniye cinsinden)
            video_frame.pts = int(now_seconds * 1000)

            self.loop.call_soon_threadsafe(self.queue.put_nowait, video_frame)

    async def recv(self):
        return await self.queue.get()

    def stop(self):
        self._running = False
        self.cap.release()

class WebRTCManager:
    """
    P2P ses bağlantısını yönetir (SoundDeviceAudioTrack kullanarak).
    """

    def __init__(self, master_app, target_username):
        self.camera_track = None
        self.master_app = master_app
        self.target_username = target_username

        # STUN sunucusu ekliyoruz
        config = RTCConfiguration(
            iceServers=[
                RTCIceServer(urls="stun:stun.l.google.com:19302"),
                RTCIceServer(urls="turn:your.turn.server:3478", username="user", credential="pass")

            ]
        )
        self.pc = RTCPeerConnection(configuration=config)

        self.loop = master_app.asyncio_loop
        self.speaker_stream = None
        self.speaker_task = None
        self.mic_track = SoundDeviceAudioTrack(master_app.asyncio_loop)

        @self.pc.on("connectionstatechange")
        def on_connectionstatechange():
            print(f"WebRTC Bağlantı Durumu ({self.target_username}): {self.pc.connectionState}")
            window = self.master_app.private_chat_windows.get(self.target_username)
            if not window:
                return

            new_status_text = ""
            if self.pc.connectionState == "connected":
                new_status_text = "📞 Bağlandı (Ses Aktif)"
            elif self.pc.connectionState == "failed":
                new_status_text = "❌ Bağlantı Başarısız"
                self.master_app.schedule_gui_update(window.end_call, notify_server=False)
            elif self.pc.connectionState == "disconnected":
                new_status_text = "⚠️ Bağlantı Zayıf"
            elif self.pc.connectionState == "closed":
                new_status_text = "Arama Kapatıldı."

            if new_status_text:
                self.master_app.schedule_gui_update(window.call_status_label.configure, text=new_status_text)



        # 🔊 Karşıdan ses geldiğinde hoparlörü aç
        @self.pc.on("track")
        def on_track(track):
            print(f"DEBUG ({self.target_username}): Track alındı, kind={track.kind}")
            if track.kind == "audio":
                print(f"DEBUG ({self.target_username}): Ses track'i alındı.")

                self.speaker_stream = sd.OutputStream(
                    samplerate=48000,
                    channels=1,
                    dtype='int16',
                    blocksize=1024
                )
                self.speaker_stream.start()
                self.speaker_task = asyncio.ensure_future(self.run_speaker(track))

            if track.kind == "video":
                print(f"DEBUG ({self.target_username}): Video track alındı.")
                window = self.master_app.private_chat_windows.get(self.target_username)
                if window:
                    asyncio.ensure_future(window.run_video(track))
                else:
                    print(f"DEBUG ({self.target_username}): Video track için pencere bulunamadı.")

        # ❄ ICE candidate üretildiğinde sunucuya gönder
        @self.pc.on("icecandidate")
        def on_icecandidate(event):
            if event.candidate:
                self.send_signal("CALL_CANDIDATE", event.candidate.to_sdp())

    async def add_camera_track(self, use_dummy=False):
        if not hasattr(self, "camera_track") or self.camera_track is None:
                self.camera_track = CameraVideoTrack(self.loop)
                print(f"DEBUG ({self.target_username}): Gerçek kamera track eklendi.")
                self.pc.addTrack(self.camera_track)
        else:
            print(f"DEBUG ({self.target_username}): Kamera track zaten mevcut.")

    async def renegotiate(self):
        print(f"DEBUG ({self.target_username}): Renegotiation başlatılıyor...")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        print(f"DEBUG ({self.target_username}): LocalDescription offer ayarlandı.")
        self.send_signal("CALL_OFFER", offer.sdp)

    async def remove_camera_track(self):
        if hasattr(self, "camera_track") and self.camera_track:
            self.camera_track.stop()
            senders = [s for s in self.pc.getSenders() if s.track == self.camera_track]
            for sender in senders:
                try:
                    await sender.replaceTrack(None) # <-- DÜZELTME
                except Exception as e:
                    print(f"HATA (removeTrack): {e}")
            self.camera_track = None
            print(f"DEBUG ({self.target_username}): Kamera track kaldırıldı.")

    async def run_speaker(self, track):
        """Gelen ses çerçevelerini hoparlöre yazar."""
        print(f"DEBUG ({self.target_username}): Hoparlör görevi başlatıldı.")
    async def blocking_write(data):
        try:
            self.speaker_stream.write(data)
        except Exception as e:
                print(f"HATA (blocking_write): {e}")

        try:
            while True:
                frame = await track.recv()
                arr = frame.to_ndarray(format='s16')  # ndarray
                if self.speaker_stream.channels == 2 and arr.ndim == 1:
                    arr = np.repeat(arr[:, np.newaxis], 2, axis=1)
                if self.speaker_stream.channels == 1 and arr.ndim == 2:
                    arr = np.mean(arr, axis=1).astype(np.int16)


        except asyncio.CancelledError:
            print(f"DEBUG ({self.target_username}): Hoparlör görevi durduruldu.")
        except Exception as e:
            print(f"Hoparlör akış hatası: {e}")



    async def add_mic_track(self):
        if not self.mic_track:
            print("HATA: Mikrofon yok.")
            return

        # --- YENİ KORUMA ---
        # Bu track'i gönderen bir sender (verici) zaten var mı?
        senders = [s for s in self.pc.getSenders() if s.track == self.mic_track]
        if senders:
            print(f"DEBUG ({self.target_username}): Mikrofon track ZATEN eklenmiş, 'start' kontrol ediliyor...")
            await self.mic_track.start()  # Sadece 'start' olduğundan emin ol
            return
        # --- KORUMA SONU ---

        print(f"DEBUG ({self.target_username}): Mikrofon ekleniyor...")
        self.pc.addTrack(self.mic_track)
        await self.mic_track.start()
        print(f"DEBUG ({self.target_username}): Mikrofon eklendi ve hazır.")

    def send_signal(self, command, sdp_or_candidate):
        self.master_app.send_call_signal(command, self.target_username, {"sdp": sdp_or_candidate})

    async def create_offer(self):
        await self.add_mic_track()



        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self.send_signal("CALL_OFFER", offer.sdp)


    async def handle_offer(self, offer_sdp):
            offer_desc = RTCSessionDescription(sdp=offer_sdp, type="offer")
            await self.pc.setRemoteDescription(offer_desc)

            await self.add_mic_track()
            await self.add_camera_track()

            answer = await self.pc.createAnswer()
            # ...
            await self.pc.setLocalDescription(answer)
            self.send_signal("CALL_ANSWER", answer.sdp)

    async def handle_answer(self, answer_sdp):
        answer_desc = RTCSessionDescription(sdp=answer_sdp, type="answer")
        await self.pc.setRemoteDescription(answer_desc)
        print(f"DEBUG ({self.target_username}): P2P el sıkışma tamamlandı.")

    async def add_ice_candidate_sdp(self, candidate_sdp: str):
        try:
            cand = candidate_from_sdp(candidate_sdp)
            await self.pc.addIceCandidate(cand)
        except Exception as e:
            print(f"HATA (ICE): Aday eklenemedi: {e}")

    async def stop_media(self):
        if self.speaker_task:
            self.speaker_task.cancel()
            self.speaker_task = None
        if self.speaker_stream:
            self.speaker_stream.stop()
            self.speaker_stream.close()
            self.speaker_stream = None
        if self.camera_track:
            self.camera_track.stop()
            self.camera_track = None

        if self.mic_track:
            self.mic_track.stop()
            self.mic_track = None

    async def close(self):
        await self.stop_media()
        await self.pc.close()


class PrivateChatWindow(ctk.CTkToplevel):
    """
    Belirli bir kullanıcıyla yapılan özel sohbet için
    açılır pencere sınıfı.
    """

    # PrivateChatWindow sınıfı içinde
    def __init__(self, master, target_username):
        super().__init__(master)
        self.master_app = master  # Bu, ana ChatApp sınıfıdır
        self.target_username = target_username
        self.rtc_manager = WebRTCManager(self.master_app, self.target_username)

        self.title(f"Özel Mesaj: {self.target_username}")
        self.geometry("350x450")
        self.video_enabled = False
        self.video_state = "idle"  # idle | pending_incoming | pending_outgoing | active
        self._video_dialog_buttons = None

        # --- YERLEŞİM (GRID) YAPILANDIRMASI ---
        # 3 satırımız var:
        # Satır 0: Arama butonları (sabit)
        # Satır 1: Sohbet kutusu (genişleyecek)
        # Satır 2: Mesaj girişi (sabit)
        # ...
        # 4 satırımız var:
        # Satır 0: Arama butonları (sabit)
        # Satır 1: Sohbet kutusu (genişleyecek)
        # Satır 2: Video label (genişleyecek) <-- YENİ
        # Satır 3: Mesaj girişi (sabit) <-- YENİ
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)  # <-- VİDEO İÇİN YENİ SATIR
        self.grid_rowconfigure(3, weight=0)  # <-- MESAJ GİRİŞİ İÇİN YENİ SATIR
        self.grid_columnconfigure(0, weight=1)
        # ...

        # --- WIDGET'LAR ---


        self.video_label = ctk.CTkLabel(self, text="",fg_color="transparent",height=100)
        self.video_label.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=10, pady=10)



        # 1. Üst Çerçeve (Arama Butonları için)
        self.top_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.top_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(5, 0))

        self.video_frame = ctk.CTkFrame(self.top_frame,fg_color="transparent")
        self.video_frame.pack(side="right", padx=5)

        self.video_button = ctk.CTkButton(
            self.video_frame,
            text="📷 Kamera",
            width=80,
            command=self.toggle_video
        )
        self.video_button.pack(side="right", padx=5)

        self.call_status_label = ctk.CTkLabel(self.top_frame, text="")
        self.call_status_label.pack(side="left", padx=5)

        self.call_button = ctk.CTkButton(self.top_frame, text="📞 Ara", width=80,
                                         command=self.initiate_call)
        self.call_button.pack(side="right", padx=5)

        self.end_call_button = ctk.CTkButton(self.top_frame, text="❌ Bitir", width=80,
                                             fg_color="#E74C3C", hover_color="#C0392B",
                                             command=self.end_call)
        # (self.end_call_button.pack() <-- Başlangıçta gizli)

        # 2. Sohbet Kutusu
        self.chat_box = ctk.CTkTextbox(self, state="disabled", wrap="word")
        self.chat_box.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=10, pady=(5, 5))

        # 3. Mesaj Girişi
        self.message_entry = ctk.CTkEntry(self, placeholder_text="Mesajınızı yazın...")
        self.message_entry.grid(row=3, column=0, sticky="ew", padx=(10, 5), pady=10)
        self.message_entry.bind("<Return>", self.send_message_event)

        # 4. Gönder Butonu
        self.send_button = ctk.CTkButton(self, text="Gönder", width=70, command=self.send_message_event)
        self.send_button.grid(row=3, column=1, sticky="e", padx=(0, 10), pady=10)

        # Pencere kapatıldığında ana listeyi bilgilendir
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def initiate_call(self):
            """'Ara' butonuna basıldığında."""
            self.call_status_label.configure(text=f"{self.target_username} aranıyor...")
            self.call_button.pack_forget()  # Ara butonunu gizle
            self.end_call_button.pack(side="right", padx=5)  # Bitir butonunu göster

            # Ana uygulamaya (ChatApp) sunucuya göndermesi için sinyal ver
            self.master_app.send_call_signal("CALL_REQUEST", self.target_username)

    def set_call_ui_to_active(self, status_text="Arama bağlandı! (P2P kuruluyor...)"):
        """Arayüzü 'arama-içi' durumuna geçirir (Butonları günceller)."""
        self.call_status_label.configure(text=status_text)
        self.call_button.pack_forget()  # Ara butonunu gizle
        self.end_call_button.pack(side="right", padx=5)  # Bitir butonunu göster

    def end_call(self, notify_server=True):
            """'Bitir' butonuna basıldığında veya arama bittiğinde."""
            self.call_status_label.configure(text="Arama sonlandırıldı.")
            self.end_call_button.pack_forget()  # Bitir butonunu gizle
            self.call_button.pack(side="right", padx=5)  # Ara butonunu göster
            self.master_app.run_coroutine_threadsafe(self.rtc_manager.close())

            if notify_server:
                # Ana uygulamaya (ChatApp) sunucuya göndermesi için sinyal ver
                self.master_app.send_call_signal("CALL_ENDED", self.target_username)

        # --- DIŞARIDAN KONTROL FONKSİYONLARI ---
        # Bu fonksiyonlar ana ChatApp tarafından çağrılacak

    # PrivateChatWindow sınıfı içinde
    def toggle_video(self):
        # Debounce: bir işlem zaten bekliyorsa ikinciyi başlatma
        if self.video_state in ("pending_incoming", "pending_outgoing"):
            self.call_status_label.configure(text="📷 Görüntülü arama isteği beklemede...")
            return

        if not self.video_enabled:
            # İstek yolla, kabul bekle
            self.video_state = "pending_outgoing"
            self.master_app.send_call_signal("VIDEO_REQUEST", self.target_username)
            self.video_button.configure(text="📷 Kapat")
        else:
            # Kapat
            self.video_state = "idle"
            self.video_enabled = False
            self.master_app.send_call_signal("VIDEO_ENDED", self.target_username)
            self.master_app.run_coroutine_threadsafe(self.rtc_manager.remove_camera_track())
            self.call_status_label.configure(text="📷 Görüntülü arama kapatıldı")
            self.video_button.configure(text="📷 Kamera")

    def on_video_request(self):
        # Zaten aktifse veya dışa dönük istek bekliyorsak ikinci diyalog açma
        if self.video_state in ("active", "pending_outgoing"):
            self.master_app.send_call_signal("VIDEO_REJECT", self.target_username)
            return

        self.video_state = "pending_incoming"
        self.call_status_label.configure(text="📷 Görüntülü arama isteği geldi")

        # Mevcut buton grubu varsa yeniden oluşturma
        if self._video_dialog_buttons is None:
            container = ctk.CTkFrame(self.top_frame, fg_color="transparent")
            container.pack(side="right", padx=5)
            btn_accept = ctk.CTkButton(container, text="Kabul Et", command=self.accept_video)
            btn_reject = ctk.CTkButton(container, text="Reddet", command=self.reject_video)
            btn_accept.pack(side="left", padx=3)
            btn_reject.pack(side="left", padx=3)
            self._video_dialog_buttons = container



    def accept_video(self):
        if self.video_state != "pending_incoming":
            return

        # 1. Karşı tarafa kabul ettiğimizi bildiriyoruz
        self.master_app.send_call_signal("VIDEO_ACCEPT", self.target_username)

        # 2. Biz (kabul eden taraf) kendi kameramızı ekliyoruz
        self.master_app.run_coroutine_threadsafe(self.rtc_manager.add_camera_track())  #

        # --- DÜZELTME BURADA ---
        # 3. Müzakereyi BİZ (kabul eden taraf) başlatıyoruz.
        # Kodunuzdaki [cite: 62] ve [cite: 65]'teki mantığın aksine,
        # bu satırı EKLEYEREK yeni 'Offer'ı biz gönderiyoruz:
        print(f"DEBUG ({self.target_username}): Video kabul edildi, yeniden müzakere (renegotiate) başlatılıyor...")
        self.master_app.run_coroutine_threadsafe(self.rtc_manager.renegotiate())
        # --- DÜZELTME SONU ---

        self.video_enabled = True
        self.video_state = "active"
        self.call_status_label.configure(text="📷 Görüntülü arama başladı")
        self._dispose_video_dialog_buttons()

    def on_video_accepted_by_peer(self):
        if self.video_state != "pending_outgoing":
            return

        # 1. Arayan taraf olarak kameramızı ekliyoruz.
        self.master_app.run_coroutine_threadsafe(self.rtc_manager.add_camera_track())

        # 2. Müzakereyi (renegotiate) BİZ BAŞLATMIYORUZ.
        #    Aramayı kabul eden (alıcı) tarafın bize OFFER göndermesini bekleyeceğiz.
        # self.master_app.run_coroutine_threadsafe(self.rtc_manager.renegotiate()) # <--- BU SATIRI SİLDİK

        # 3. Durumu "aktif" olarak ayarla
        self.video_enabled = True
        self.video_state = "active"
        self.call_status_label.configure(text="📷 Görüntülü arama başladı (Bağlanıyor...)")


    def on_video_rejected_by_peer(self):
        if self.video_state != "pending_outgoing":
            return
        self.video_state = "idle"
        self.video_enabled = False
        self.call_status_label.configure(text="📷 Görüntülü arama reddedildi")
        self.video_button.configure(text="📷 Kamera")

    def reject_video(self):
        if self.video_state != "pending_incoming":
            return
        self.master_app.send_call_signal("VIDEO_REJECT", self.target_username)
        self.video_state = "idle"
        self.call_status_label.configure(text="📷 Görüntülü arama reddedildi")
        self._dispose_video_dialog_buttons()

    def _dispose_video_dialog_buttons(self):
        if self._video_dialog_buttons:
            try:
                self._video_dialog_buttons.destroy()
            except:
                pass
            self._video_dialog_buttons = None

    def on_call_accepted(self):
        """SADECE ARAYAN KİŞİ tarafından (kabul bildirimi alındığında) çağrılır."""

        # 1. Arayüzü "arama-içi" duruma geçir
        self.set_call_ui_to_active()

        # 2. El sıkışmayı (handshake) başlatmak için bir 'Teklif' (Offer) oluştur
        print(f"DEBUG ({self.target_username}): Arama kabul edildi, P2P 'Teklif' (Offer) gönderiliyor...")
        self.master_app.run_coroutine_threadsafe(self.rtc_manager.create_offer())

    def on_call_rejected(self):
            """Karşı taraf aramayı reddettiğinde."""
            self.call_status_label.configure(text="Arama reddedildi.")
            self.end_call(notify_server=False)  # Sadece UI'ı sıfırla

            # ... PrivateChatWindow sınıfı içinde ...



    async def run_video(self, track):
        """
        Gelen video akışını alır ve CTkLabel'da (video_label) görüntüler.
        """
        print(f"DEBUG ({self.target_username}): run_video coroutine'i BAŞLADI. Video bekleniyor...")
        try:
            while True:
                frame = await track.recv()  # av.VideoFrame
                img = frame.to_ndarray(format="bgr24")
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(img)

                # Debug: kare bilgisi
                pts = getattr(frame, "pts", None)
                h, w = img.shape[:2]
                print(f"DEBUG ({self.target_username}): Yeni frame (pts={pts}) size=({w}x{h})")

                def update_gui(pil_img_copy=pil_img):
                    try:
                        # CTkImage kullan → HighDPI destekli
                        tk_img = CTkImage(light_image=pil_img_copy, size=(w, h))
                        self.video_label.configure(image=tk_img)
                        self.video_label.image = tk_img  # referansı sakla
                    except Exception as e:
                        print(f"DEBUG ({self.target_username}): GUI güncelleme hatası: {e}")

                # Ana thread'te GUI güncellemesi
                self.master_app.schedule_gui_update(update_gui)

        except asyncio.CancelledError:
            print(f"DEBUG ({self.target_username}): run_video coroutine'i durduruldu.")
        except Exception as e:
            print(f"DEBUG ({self.target_username}): Video akışı durdu veya hata verdi: {e}")

            def clear_video_label():
                try:
                    self.video_label.configure(image=None)
                    self.video_label.image = None
                except:
                    pass

            self.master_app.schedule_gui_update(clear_video_label)

    def on_call_ended_by_peer(self):
            """Karşı taraf aramayı kapattığında."""
            self.call_status_label.configure(text="Karşı taraf kapattı.")
            self.end_call(notify_server=False)  # Sadece UI'ı sıfırla

    def send_message_event(self, event=None):
        message = self.message_entry.get()
        if not message:
            return

        # Ana uygulama üzerinden mesajı gönder
        self.master_app.send_dm_from_window(self.target_username, message)
        self.add_message_to_window(f"[Siz -> {self.target_username}]: {message}")

        # Kendi penceremize "Siz" olarak mesajı ekle

        self.message_entry.delete(0, "end")

    def add_message_to_window(self, message):
        """
        Ana uygulama veya kendisi tarafından çağrılır.
        """
        try:
            self.chat_box.configure(state="normal")
            self.chat_box.insert("end", message + "\n")
            self.chat_box.configure(state="disabled")
            self.chat_box.see("end")  # En alta kaydır
        except Exception as e:
            print(f"Özel pencereye mesaj eklenemedi: {e}")



    def on_closing(self):
        """
        Pencere kapatıldığında, ana uygulamanın sözlüğünden
        kendini kaldırır.
        """
        self.master_app.run_coroutine_threadsafe(self.rtc_manager.close())
        self.master_app.notify_private_window_closed(self.target_username)
        self.destroy()

class ChatApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Şifreli Chat (Asyncio/WebSocket Sürümü)")
        self.geometry("450x600")

        self.e2ee_sessions = {}
        # --- Asyncio ve Threading Köprüsü ---
        self.audio_frames = []

        # --- Durum Değişkenleri ---
        self.websocket = None  # Artık 'client_socket' değil
        self.nickname = ""
        self.authenticated = False

        self.private_chat_windows = {}
        # --- SOUNDDEVICE/SES AYARLARI ---
        self.audio_stream_in = None  # Kayıt stream'i
        self.audio_stream_out = None  # Çalma stream'i
        self.is_recording = False

        self.channels = 1
        self.dtype = 'int16'  # Bu, pyaudio.paInt16'nın NumPy karşılığıdır
        self.chunk = 1024

        try:
            # Doğru sorgu: 'query_devices(kind='input')['default_samplerate']'
            self.rate = int(sd.query_devices(kind='input')['default_samplerate'])
        except Exception as e:
            print(f"UYARI: Varsayılan mikrofon bulunamadı, 44100Hz varsayılıyor. Hata: {e}")
            self.rate = 44100  # Güvenli bir varsayılan

        self.MAX_RECORD_SECONDS = 10

        # --- YENİ SATIRLAR ---
        self._typing_timer = None  # 3 saniyelik "yazmayı bıraktı" zamanlayıcısı
        self._am_i_typing = False  # Sunucuya gereksiz 'START' komutu göndermemek için
        self.who_is_typing = set()  # Kimlerin yazdığını tutan liste
        # --- YENİ SATIRLAR SONU ---
        # --- Asyncio ve Threading Köprüsü ---
        self.asyncio_loop = asyncio.new_event_loop()  # Arka plan thread'i için yeni bir event loop
        self.queue = asyncio.Queue()  # Arayüzden -> Asyncio'ya komut göndermek için
        self._sound_cooldown_timer_in = None  # Gelen mesajlar için
        self._sound_cooldown_timer_out = None  # Giden mesajlar için
        # --- GÜNCELLENMİŞ KISIM ---
        self.load_icons()  # İkonları yükle
        self.start_asyncio_thread()
        self.create_auth_ui()
        # --- GÜNCELLENMİŞ KISIM SONU ---

        # ChatApp sınıfı içinde
    def send_call_signal(self, command, target_user, data_payload=None):
            """Genel amaçlı arama sinyali gönderici. (Güncellendi)"""

            # Temel yükü (payload) oluştur
            payload_content = {"target": target_user}

            # Eğer ekstra veri (örn: SDP) varsa, onu da yüke ekle
            if data_payload:
                payload_content.update(data_payload)

            payload_json = {"command": command, "payload": payload_content}
            self.run_coroutine_threadsafe(self.send_json_to_server(payload_json))

    def open_private_chat(self, target_username):
        """Kullanıcı listesinden birine tıklandığında çağrılır."""
        if not target_username or target_username == "None":
            print("DEBUG: open_private_chat() hatalı çağrı — target boş, iptal.")
            return

        # Kendinle konuşma
        if target_username == self.nickname:
            print("DEBUG: Kendinizle özel sohbet açamazsınız.")
            return

        # Pencere zaten açık mı?
        if target_username in self.private_chat_windows:
            # Açıksa, öne getir
            self.private_chat_windows[target_username].lift()



        else:
            # Değilse, yenisini oluştur ve kaydet
            try:
                new_window = PrivateChatWindow(master=self, target_username=target_username)
                self.private_chat_windows[target_username] = new_window
                self.start_e2ee_handshake_with(target_username)


                print(f"DEBUG: {target_username} için DM geçmişi isteniyor...")
                payload_json = {
                    "command": "FETCH_DM_HISTORY",
                    "payload": {"target": target_username}
                }
                self.run_coroutine_threadsafe(self.send_json_to_server(payload_json))

            except Exception as e:
                print(f"Özel pencere oluşturulamadı: {e}")

    def send_dm_from_window(self, target_user, message):

        sess = self.e2ee_sessions.get(target_user)
        if sess and "aes_key" in sess:
            from crypto_e2ee import seal
            aad = f"[{self.nickname}->{target_user}]".encode("utf-8")
            nonce, ct = seal(sess["aes_key"], message.encode("utf-8"), aad=aad)
            payload_json = {
                "command": "ENC_MSG",
                "payload": {
                    "target": target_user,
                    "nonce": base64.b64encode(nonce).decode("utf-8"),
                    "salt": base64.b64encode(sess["salt"]).decode("utf-8"),
                    "ct": base64.b64encode(ct).decode("utf-8"),
                    "aad": base64.b64encode(aad).decode("utf-8"),
                }
            }
        else:
            payload_json = {"command": "DM", "payload": {"target": target_user, "message": message}}

        self.run_coroutine_threadsafe(self.send_json_to_server(payload_json))
        self.play_outgoing_sound()

    def notify_private_window_closed(self, target_username):
        """Özel pencere kapatıldığında çağrılır."""
        self.private_chat_windows.pop(target_username, None)
        print(f"DEBUG: {target_username} ile özel sohbet kapatıldı.")

    def load_icons(self):
        """Uygulama için gerekli ikonları yükler."""
        try:

            self.user_icon = ctk.CTkImage(Image.open(resource_path("assets/user_icon.png")), size=(24, 24))
            self.lock_icon = ctk.CTkImage(Image.open(resource_path("assets/lock_icon.png")), size=(24, 24))
            self.send_icon = ctk.CTkImage(Image.open(resource_path("assets/send_icon.png")), size=(24, 24))
            self.server_icon = ctk.CTkImage(Image.open(resource_path("assets/server_icon.png")), size=(24, 24))
        except FileNotFoundError as e:
            print(f"Hata: İkon dosyaları 'assets' klasöründe bulunamadı: {e}")
            print("İkonsuz devam ediliyor...")
            # Hata durumunda boş ikonlar oluştur
            self.user_icon = None
            self.lock_icon = None
            self.server_icon = None
            self.send_icon = None

    # --- YENİ FONKSİYON SONU ---

    def start_asyncio_thread(self):
        """Asyncio event loop'u ayrı bir thread'de başlatır."""

        def run_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=run_loop, args=(self.asyncio_loop,), daemon=True)
        t.start()
        print("DEBUG: Asyncio arka plan thread'i başlatıldı.")

    def run_coroutine_threadsafe(self, coro):
        """Ana thread'den (GUI) asyncio thread'ine güvenle coroutine göndermeyi sağlar."""
        return asyncio.run_coroutine_threadsafe(coro, self.asyncio_loop)

    def schedule_gui_update(self, func, *args, **kwargs):
        """Asyncio thread'inden ana GUI thread'ine güvenle fonksiyon çağırmayı sağlar."""
        self.after(0, func, *args,**kwargs)

    # --- Arayüz Fonksiyonları (Çoğunlukla Aynı) ---

        # 'create_auth_ui' fonksiyonunuzu TAMAMEN bununla değiştirin:
        # ChatApp sınıfının içine, diğer def fonksiyonlarıyla aynı hizaya EKLE:

    def show_auth_error(self, message):
            """Giriş/Kayıt ekranındaki hata etiketini günceller."""
            try:
                # Not: Bu fonksiyon, mesaj 'başarılı' içeriyorsa rengi yeşile çevirir
                self.auth_error_label.configure(text=message,
                                                text_color="red" if "başarılı" not in message else "green")
            except:
                # Arayüz (etiket) artık mevcut değilse (çok nadir) görmezden gel
                pass

    def create_auth_ui(self):
            """Giriş Yap / Kayıt Ol arayüzünü .grid() kullanarak oluşturur."""

            self.clear_widgets()
            self.geometry("450x600")
            self.title("Giriş Yap veya Kayıt Ol")

            # --- Izgara (Grid) Yapılandırması ---
            # Ana pencereyi, ızgara sistemi için yapılandır
            # Satır 0 (tab_view) genişlesin (weight=1)
            self.grid_rowconfigure(0, weight=1)
            # Satır 1 (server_frame) sabit kalsın (weight=0, varsayılan)
            # Satır 2 (auth_error_label) sabit kalsın (weight=0, varsayılan)
            # Sütun 0 (tüm içerik) genişlesin (weight=1)
            self.grid_columnconfigure(0, weight=1)
            # --- Izgara Sonu ---

            self.tab_view = ctk.CTkTabview(self, width=400)
            self.tab_view.grid(row=0, column=0, pady=10, padx=20, sticky="nsew")  # nsew = her yöne genişle

            self.tab_view.add("Giriş Yap")
            self.tab_view.add("Kayıt Ol")

            # --- Giriş Yap Sekmesi ---
            login_frame = self.tab_view.tab("Giriş Yap")
            login_frame.grid_columnconfigure(0, weight=1)  # İçerik merkezi kalsın

            # İkonlu Giriş Kutusu Çerçevesi (Username)
            username_frame_login = ctk.CTkFrame(login_frame, fg_color="transparent")
            username_frame_login.grid(row=0, column=0, pady=(40, 10))
            if self.user_icon:  # İkon yüklendiyse
                ctk.CTkLabel(username_frame_login, image=self.user_icon, text="").pack(side="left", padx=5)
            self.username_entry_login = ctk.CTkEntry(username_frame_login, placeholder_text="Kullanıcı Adı", width=300)
            self.username_entry_login.pack(side="left")

            # İkonlu Giriş Kutusu Çerçevesi (Password)
            password_frame_login = ctk.CTkFrame(login_frame, fg_color="transparent")
            password_frame_login.grid(row=1, column=0, pady=10)
            if self.lock_icon:  # İkon yüklendiyse
                ctk.CTkLabel(password_frame_login, image=self.lock_icon, text="").pack(side="left", padx=5)
            self.password_entry_login = ctk.CTkEntry(password_frame_login, placeholder_text="Şifre", show="*",
                                                     width=300)
            self.password_entry_login.pack(side="left")

            self.login_button = ctk.CTkButton(login_frame, text="Giriş Yap", command=self.handle_login, width=300)
            self.login_button.grid(row=2, column=0, pady=20)

            # --- Kayıt Ol Sekmesi ---
            register_frame = self.tab_view.tab("Kayıt Ol")
            register_frame.grid_columnconfigure(0, weight=1)  # İçerik merkezi kalsın

            # İkonlu Giriş Kutusu Çerçevesi (Username)
            username_frame_reg = ctk.CTkFrame(register_frame, fg_color="transparent")
            username_frame_reg.grid(row=0, column=0, pady=(20, 10))
            if self.user_icon:
                ctk.CTkLabel(username_frame_reg, image=self.user_icon, text="").pack(side="left", padx=5)
            self.username_entry_register = ctk.CTkEntry(username_frame_reg, placeholder_text="Kullanıcı Adı", width=300)
            self.username_entry_register.pack(side="left")

            # İkonlu Giriş Kutusu Çerçevesi (Password)
            password_frame_reg = ctk.CTkFrame(register_frame, fg_color="transparent")
            password_frame_reg.grid(row=1, column=0, pady=10)
            if self.lock_icon:
                ctk.CTkLabel(password_frame_reg, image=self.lock_icon, text="").pack(side="left", padx=5)
            self.password_entry_register = ctk.CTkEntry(password_frame_reg, placeholder_text="Şifre", show="*",
                                                        width=300)
            self.password_entry_register.pack(side="left")

            # İkonlu Giriş Kutusu Çerçevesi (Confirm)
            password_frame_conf = ctk.CTkFrame(register_frame, fg_color="transparent")
            password_frame_conf.grid(row=2, column=0, pady=10)
            if self.lock_icon:
                ctk.CTkLabel(password_frame_conf, image=self.lock_icon, text="").pack(side="left", padx=5)
            self.password_entry_confirm = ctk.CTkEntry(password_frame_conf, placeholder_text="Şifre (Tekrar)", show="*",
                                                       width=300)
            self.password_entry_confirm.pack(side="left")

            self.register_button = ctk.CTkButton(register_frame, text="Kayıt Ol", command=self.handle_register,
                                                 width=300)
            self.register_button.grid(row=3, column=0, pady=20)

            # --- Sunucu Bilgileri (Altta, Ortak) ---
            self.server_frame = ctk.CTkFrame(self)
            self.server_frame.grid(row=1, column=0, pady=10, padx=20, sticky="ew")  # ew = doğu-batı yönünde genişle
            self.server_frame.grid_columnconfigure(1, weight=1)  # Entry'nin genişlemesi için

            if self.server_icon:
                ctk.CTkLabel(self.server_frame, image=self.server_icon, text="").grid(row=0, column=0, padx=5)

            self.server_entry = ctk.CTkEntry(self.server_frame, placeholder_text="Sunucu Adresi (IP)")
            self.server_entry.insert(0, "127.0.0.1")
            self.server_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5))

            self.port_entry = ctk.CTkEntry(self.server_frame, width=80)
            self.port_entry.insert(0, "50505")
            self.port_entry.grid(row=0, column=2, sticky="e")

            # Hata Mesajları için Etiket
            self.auth_error_label = ctk.CTkLabel(self, text="", text_color="red")
            self.auth_error_label.grid(row=2, column=0, pady=5, padx=20, sticky="ew")

    def handle_login(self):
        """Giriş komutunu ve bağlantı bilgilerini hazırlar, async işleyiciye gönderir."""
        username = self.username_entry_login.get()
        password = self.password_entry_login.get()
        host = self.server_entry.get()
        port = self.port_entry.get()

        if not username or not password or not host or not port:
            self.show_auth_error("Tüm alanlar doldurulmalıdır.")
            return

        # --- EKLENEN BLOK ---
        # Hata 1'i düzeltir: Butonları kilitle ve geri bildirim ver
        self.set_auth_buttons_state("disable")
        self.show_auth_error("Giriş yapılıyor...")
        # --- EKLENEN BLOK SONU ---

        # Sunucuya gönderilecek İLK komutu hazırla
        payload_json = {"command": "LOGIN", "payload": {"user": username, "pass": password}}

        # Async motora "Bağlan ve bu ilk komutu gönder" görevini ver
        self.run_coroutine_threadsafe(self.connect_and_process(host, port, payload_json))


    def handle_register(self):
        username = self.username_entry_register.get()
        password = self.password_entry_register.get()
        confirm = self.password_entry_confirm.get()
        host = self.server_entry.get()
        port = self.port_entry.get()

        if not username or not password or not confirm or not host or not port: self.show_auth_error("Tüm alanlar doldurulmalıdır."); return
        if password != confirm: self.show_auth_error("Şifreler uyuşmuyor."); return
        if len(password.encode('utf-8')) > 72: self.show_auth_error("Şifre çok uzun (Maks. 72 byte)."); return

        self.set_auth_buttons_state("disable")
        self.show_auth_error("Bağlanılıyor...")

        payload_json = {"command": "REGISTER", "payload": {"user": username, "pass": password}}

    # KRİTİK DÜZELTME: Doğru fonksiyon adı kullanılmalı
        self.run_coroutine_threadsafe(self.connect_and_process(host, port, payload_json))



    async def send_json_to_server(self, data):
        """JSON verisini string'e çevirir ve websocket üzerinden gönderir."""
        # 'if self.websocket:' kontrolünü kaldırıyoruz,
        # çünkü bu fonksiyon artık sadece 'websocket'in var olduğu
        # güvenli bir bağlamda (context) çağrılacak.
        try:
            await self.websocket.send(json.dumps(data))
        except Exception as e:
            # Bağlantı tam o anda koptuysa
            print(f"HATA: Gönderilemedi, bağlantı muhtemelen kapandı: {e}")
            self.schedule_gui_update(self.go_back_to_login, "Bağlantı koptu, gönderilemedi.")

    async def connect_and_process(self, host, port, initial_payload_json):
        """Sunucuya bağlanır, İLK komutu gönderir ve dinlemeye başlar."""

        # --- wss:// kullan--
        uri = f"wss://{host}:{port}"

        # --- DÜZELTME 2: 'ws://' 'ssl=None' gerektirir. ---
        # 'ssl_context' oluşturan tüm satırları siliyoruz
        # ve 'ssl_param'i manuel olarak 'None' yapıyoruz.
        ssl_param = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_param.check_hostname = False
        ssl_param.verify_mode = ssl.CERT_NONE

        print("DEBUG: Güvensiz (ws://) bağlantı deneniyor...")

        try:
            # --- DÜZELTME 3: 'ssl=ssl_context' yerine 'ssl=ssl_param' (None) kullanın ---
            async with websockets.connect(uri, ssl=ssl_param) as websocket:
                self.websocket = websocket
                print(f"DEBUG: {uri} adresine bağlanıldı.")
                self.schedule_gui_update(self.show_auth_error, "Bağlanıldı, giriş yapılıyor...")

                # 2. Bağlantı TAMAMLANDIKTAN SONRA, ilk komutu gönder
                await self.send_json_to_server(initial_payload_json)

                # 3. Komut gönderildikten SONRA, cevapları dinlemeye başla
                async for message in websocket:
                    self.schedule_gui_update(self.handle_server_message, message)

        except asyncio.TimeoutError:
            self.schedule_gui_update(self.go_back_to_login, "Bağlantı zaman aşımına uğradı (Sunucu/Firewall).")
        except websockets.exceptions.InvalidURI:
            self.schedule_gui_update(self.go_back_to_login, "Hata: Geçersiz Sunucu Adresi/Portu.")
        # WSS/SSL el sıkışma hatası
        except ssl.SSLError as e:
            print(f"SSL Hatası: {e}", file=sys.stderr)
            self.schedule_gui_update(self.go_back_to_login, "Güvenlik (SSL) hatası. Sunucu sertifikası geçersiz.")
        except (OSError, websockets.exceptions.ConnectionClosed) as e:
            print(f"DEBUG: Bağlantı kesildi veya kurulamadı: {e}")
            self.schedule_gui_update(self.go_back_to_login, f"Sunucuya bağlanılamadı: {e}")
        except Exception as e:
            print(f"DEBUG: Beklenmedik websocket hatası: {e}")
            traceback.print_exc(file=sys.stderr)
            self.schedule_gui_update(self.go_back_to_login, f"Bilinmeyen bir hata oluştu: {e}")
        finally:
            # Dinleme döngüsü biterse (bağlantı koparsa)
            self.websocket = None
            self.authenticated = False
            print("DEBUG: connect_and_process sonlandı, bağlantı sıfırlandı.")
            self.schedule_gui_update(self.set_auth_buttons_state, "normal")

    def handle_server_message(self, message_str):
        """Sunucudan gelen JSON mesajını (string) ayrıştırır ve ilgili GUI fonksiyonunu çağırır."""
        global derive_aes_key
        try:
            data = json.loads(message_str)
            command = data.get("command")
            payload = data.get("payload")

            # Sunucunun yeni protokolüne (JSON) göre yönlendirme

            if command == "LOGIN_DATA_PACKAGE":
                self.schedule_gui_update(self.set_auth_buttons_state, "normal")
                self.transition_to_chat(payload)


            elif command in ["LOGIN_FAIL", "REGISTER_SUCCESS", "REGISTER_FAIL","AUTH_FAIL"]:

                if command == "REGISTER_SUCCESS":

                    self.show_auth_error(f"{payload} Lütfen şimdi giriş yapın.")

                else:

                    self.show_auth_error(payload)

                self.set_auth_buttons_state("normal")


            if command == "TYPING_START":
                self.update_typing_status(payload, is_typing=True)  # payload = "username"

            elif command == "TYPING_STOP":
                self.update_typing_status(payload, is_typing=False)  # payload = "username"
            # --- YENİ BLOKLAR SONU ---
            elif command == "KICK_SIGNAL":
                # --- KRİTİK EKLENTİ ---
                self.add_message_to_chatbox("SYS_MSG_ERR", payload)  # Atılma mesajını göster
                # Sıfırlamayı ana GUI thread'ine taşıyarak sorunsuz geçişi garantile
                self.schedule_gui_update(self.go_back_to_login, "Sunucudan atıldınız. Lütfen tekrar bağlanın.")
                # --- EKLENTİ SONU ---
            elif command == "AUDIO_DATA":
                file_id = payload.get("file_id")
                ct_b64 = payload.get("filedata_b64")
                ct = base64.b64decode(ct_b64)

                nonce_b64 = payload.get("nonce")
                salt_b64 = payload.get("salt")
                aad_b64 = payload.get("aad")

                if nonce_b64 and salt_b64:
                    nonce = base64.b64decode(nonce_b64)
                    salt = base64.b64decode(salt_b64)
                    aad = base64.b64decode(aad_b64) if aad_b64 else b""

                    sess = self.e2ee_sessions.get(target_user)
                    from crypto_e2ee import open_, derive_aes_key
                    if sess["salt"] != salt:
                        shared = sess["my_priv"].exchange(sess["peer_pub"])
                        sess["aes_key"] = derive_aes_key(shared, salt, self.e2ee_info_for_peer(target_user))
                        sess["salt"] = salt

                    try:
                        audio_bytes = open_(sess["aes_key"], nonce, ct, aad=aad)
                        self.play_audio_chunk(audio_bytes)
                    except Exception as e:
                        self.add_message_to_chatbox("SYS_MSG_ERR", f"E2E ses çözme hatası: {e}")
                else:
                    # fallback: şifresiz
                    self.play_audio_chunk(ct)
            # --- YENİ BLOK SONU ---
            elif command == "CALL_REQUEST":
                # Birisi bizi arıyor
                sender = payload.get("from")
                if sender:
                    # 'd_dialog' import'u için 'import customtkinter as ctk' gerekir
                    # Eğer import edilmediyse, dosyanın başına 'import customtkinter as ctk' ekleyin
                    # (Muhtemelen zaten var)

                    # Kullanıcıya sor
                    dialog = ctk.CTkInputDialog(
                        text=f"{sender} sizi arıyor...\nKabul ediyor musunuz?",
                        title="Gelen Arama",
                        # button_text="Kabul Et",  <-- BU SATIRI SİLİN
                        # button_color="#2ECC71", <-- BU SATIRI SİLİN
                        # cancel_button_color="#E74C3C", <-- BU SATIRI SİLİN

                    )

                    response = dialog.get_input()

                    if response:  # Kabul etti (Buton "OK" veya "Tamam" yazar)
                        self.send_call_signal("CALL_ACCEPT", sender)
                        # Otomatik olarak DM penceresini aç/öne getir
                        self.open_private_chat(sender)
                        if sender in self.private_chat_windows:
                            self.private_chat_windows[sender].set_call_ui_to_active()
                    else:  # Reddetti
                        self.send_call_signal("CALL_REJECT", sender)

            elif command == "CALL_ACCEPT":
                # Aradığımız kişi kabul etti
                sender = payload.get("from")
                if sender in self.private_chat_windows:
                    self.private_chat_windows[sender].on_call_accepted()

            elif command == "CALL_REJECT":
                # Aradığımız kişi reddetti
                sender = payload.get("from")
                if sender in self.private_chat_windows:
                    self.private_chat_windows[sender].on_call_rejected()

            elif command == "CALL_ENDED":
                # Karşı taraf kapattı
                sender = payload.get("from")
                if sender in self.private_chat_windows:
                    self.private_chat_windows[sender].on_call_ended_by_peer()


            elif command == "CALL_OFFER":

                # Birinden 'Teklif' (Offer) aldık (Biz 'Aranan' kişiyiz)

                sender = payload.get("from")

                sdp_data = payload.get("sdp")

                # İlgili pencerenin açık olduğundan emin ol

                if sender not in self.private_chat_windows:
                    self.open_private_chat(sender)

                if sender in self.private_chat_windows and sdp_data:
                    print(f"DEBUG ({sender}): 'Teklif' (Offer) alındı, 'Cevap' (Answer) hazırlanıyor...")

                    # İlgili pencerenin yöneticisine teklifi işlettir (Bu, 'Cevap' gönderecek)

                    rtc_manager = self.private_chat_windows[sender].rtc_manager

                    self.run_coroutine_threadsafe(rtc_manager.handle_offer(sdp_data))


            elif command == "CALL_ANSWER":

                # Gönderdiğimiz 'Teklif'e 'Cevap' (Answer) aldık (Biz 'Arayan' kişiyiz)

                sender = payload.get("from")

                sdp_data = payload.get("sdp")

                if sender in self.private_chat_windows and sdp_data:
                    print(f"DEBUG ({sender}): 'Cevap' (Answer) alındı. P2P kuruluyor...")

                    # İlgili pencerenin yöneticisine cevabı işlettir

                    rtc_manager = self.private_chat_windows[sender].rtc_manager

                    self.run_coroutine_threadsafe(rtc_manager.handle_answer(sdp_data))




            elif command == "CALL_CANDIDATE":

                sender = payload.get("from")

                # HATA 1 DÜZELTİLDİ: 'candidate' -> 'sdp'

                candidate_sdp = payload.get("sdp")

                if sender in self.private_chat_windows and candidate_sdp:
                    rtc_manager = self.private_chat_windows[sender].rtc_manager

                    # HATA 2 DÜZELTİLDİ: Çağrı async ve rtc_manager üzerinden olmalı

                    self.run_coroutine_threadsafe(

                        rtc_manager.add_ice_candidate_sdp(candidate_sdp)

                    )

            elif command == "DM_HISTORY":
                target = payload.get("target")
                history = payload.get("messages", [])
                if target in self.private_chat_windows:
                    window = self.private_chat_windows[target]
                    for msg in history:
                        window.add_message_to_window(msg)

            elif command == "KEY_INIT":
                sender = payload.get("from_user")
                peer_pub_b64 = payload.get("pub")
                salt_b64 = payload.get("salt")
                # If we don't have a session, create ephemeral keys now
                if sender not in self.e2ee_sessions:
                    self.start_e2ee_handshake_with(sender)  # creates my_priv/my_pub/salt
                # Derive and send KEY_REPLY
                self.complete_e2ee_handshake(sender, peer_pub_b64, salt_b64)

            elif command == "KEY_REPLY":


                sender = payload.get("from_user")
                peer_pub_b64 = payload.get("pub")
                salt_b64 = payload.get("salt")
                # Derive final key; do not send reply (we initiated)

                sess = self.e2ee_sessions.get(sender)
                if sess:
                    peer_pub = pubkey_from_bytes(base64.b64decode(peer_pub_b64))
                    salt = base64.b64decode(salt_b64)
                    shared = sess["my_priv"].exchange(peer_pub)
                    key = derive_aes_key(shared, salt, self.e2ee_info_for_peer(sender))
                    sess.update({"peer_pub": peer_pub, "aes_key": key, "salt": salt})
                    self.add_message_to_chatbox("SYS_MSG", f"🔐 {sender} ile E2E tamamlandı.")


            elif command == "VIDEO_REQUEST":

                sender = payload.get("from")

                if sender not in self.private_chat_windows:
                    self.open_private_chat(sender)

                window = self.private_chat_windows.get(sender)

                if window:
                    window.on_video_request()


            elif command == "VIDEO_ACCEPT":

                sender = payload.get("from")

                window = self.private_chat_windows.get(sender)

                if window:
                    # Karşı tarafın kabulü bize geldiyse, dışa-dönük isteğimiz bekliyorsa ilerle

                    window.on_video_accepted_by_peer()


            elif command == "VIDEO_REJECT":

                sender = payload.get("from")

                window = self.private_chat_windows.get(sender)

                if window:
                    window.on_video_rejected_by_peer()


            elif command == "VIDEO_ENDED":

                sender = payload.get("from")

                window = self.private_chat_windows.get(sender)

                if window:
                    window.video_state = "idle"

                    window.video_enabled = False

                    self.run_coroutine_threadsafe(window.rtc_manager.remove_camera_track())

                    window.call_status_label.configure(text="📷 Görüntülü arama kapatıldı")

                    window.video_button.configure(text="📷 Kamera")



            elif self.authenticated:





                # Giriş yapıldıktan sonra gelen diğer komutlar
                if command == "USER_LIST_UPDATE":
                    self.update_online_list_ui(payload)  # payload = ["ahmet", "zeynep"]


                elif command == "CHAT" or command == "SYS_MSG" or command == "SYS_MSG_ERR":

                    self.add_message_to_chatbox(command, payload)


                elif command == "DM":

                    # Sunucu '[Gönderen -> Siz]: Mesaj' veya '[Siz -> Hedef]: Mesaj' formatında gönderir

                    other_username = None

                    try:

                        if payload.startswith("[Siz -> "):

                            # Bu, sizin gönderdiğiniz bir mesajın onayıdır

                            other_username = payload.split(' ', 3)[2].strip(']:')

                        elif payload.startswith("["):

                            # Bu, size gelen yeni bir mesajdır

                            other_username = payload.split(' ', 1)[0].strip('[')

                    except Exception as e:

                        print(f"DM yönlendirmesi için kullanıcı adı ayrıştırılamadı: {e}")

                    if other_username:

                        # Pencereyi aç veya öne getir

                        self.open_private_chat(other_username)

                        # Mesajı ilgili pencereye ekle

                        if other_username in self.private_chat_windows:
                            self.private_chat_windows[other_username].add_message_to_window(payload)

                        # Gelen mesaj sesi çal (Sadece bize geliyorsa)

                        if not payload.startswith("[Siz -> "):
                            self.play_incoming_sound()

                    else:

                        # Bir hata olursa, eski yöntem gibi ana pencereye bas

                        self.add_message_to_chatbox("SYS_MSG_ERR", f"DM hedefi ayrıştırılamadı: {payload}")


                elif command == "ENC_MSG":
                    # Decide peer by payload context: for DM, payload['from_user'] = sender, for public chat you may carry sender too.
                    sender = payload.get("from_user")
                    nonce = base64.b64decode(payload.get("nonce"))
                    salt = base64.b64decode(payload.get("salt"))
                    ct = base64.b64decode(payload.get("ct"))
                    aad_b64 = payload.get("aad")
                    aad = base64.b64decode(aad_b64) if aad_b64 else b""

                    sess = self.e2ee_sessions.get(sender)
                    if not sess or "aes_key" not in sess:
                        self.add_message_to_chatbox("SYS_MSG_ERR", f"E2E anahtarı yok: {sender}")
                        return

                    # Optional: verify salt matches session; if not, re-derive
                    if sess["salt"] != salt:
                        from crypto_e2ee import derive_aes_key
                        shared = sess["my_priv"].exchange(sess["peer_pub"])
                        sess["aes_key"] = derive_aes_key(shared, salt, self.e2ee_info_for_peer(sender))
                        sess["salt"] = salt

                    from crypto_e2ee import open_
                    try:
                        msg = open_(sess["aes_key"], nonce, ct, aad=aad).decode("utf-8")
                        # Render like normal DM
                        self.open_private_chat(sender)
                        if sender in self.private_chat_windows:
                            self.private_chat_windows[sender].add_message_to_window(f"[{sender} -> Siz]: {msg}")
                        else:
                            self.add_message_to_chatbox("DM", f"[{sender} -> Siz]: {msg}")

                        self.play_incoming_sound()
                    except Exception as e:
                        self.add_message_to_chatbox("SYS_MSG_ERR", f"E2E çözme hatası: {e}")


            else:
                print(f"DEBUG: Kimlik doğrulanmamışken bilinmeyen komut: {command}")

        except json.JSONDecodeError:
            print(f"HATA: Sunucudan hatalı JSON alındı: {message_str}")

    # --- Arayüz Güncelleme Fonksiyonları (Güncellendi) ---

    def transition_to_chat(self, payload):
        """'Tek Dev Paket'i (payload) alır ve sohbet arayüzünü kurar."""
        try:
            username = payload.get("username")
            history_messages = payload.get("history", [])
            user_list = payload.get("user_list", [])

            self.nickname = username
            self.authenticated = True

            self.clear_widgets()
            self.geometry("650x550")
            self.title(f"Şifreli Chat - {self.nickname} (WebSocket)")
            self.create_chat_ui()  # Önce boş arayüzü kur

            # Sonra arayüzü doldur
            self.load_history_messages(history_messages)
            self.update_online_list_ui(user_list)

        except Exception as e:
            print(f"HATA: Giriş verisi (payload) işlenemedi: {e}")
            self.go_back_to_login("Giriş verisi işlenirken hata oluştu.")

    def e2ee_info_for_peer(self, peer_username: str) -> bytes:
        # Bind HKDF 'info' to stable identities
        a, b = sorted([self.nickname, peer_username])

        return f"chat-e2ee-v1:{a}:{b}".encode("utf-8")

    def start_e2ee_handshake_with(self, peer_username: str):
        # generate ephemeral pair
        from crypto_e2ee import gen_keypair
        my_priv, my_pub = gen_keypair()
        salt = os.urandom(16)
        self.e2ee_sessions[peer_username] = {"my_priv": my_priv, "my_pub": my_pub, "salt": salt}
        payload = {
            "target": peer_username,
            "pub": base64.b64encode(my_pub).decode("utf-8"),
            "salt": base64.b64encode(salt).decode("utf-8"),
        }
        self.run_coroutine_threadsafe(self.send_json_to_server({"command": "KEY_INIT", "payload": payload}))

    def complete_e2ee_handshake(self, peer_username: str, peer_pub_b64: str, salt_b64: str):
        from crypto_e2ee import pubkey_from_bytes, derive_aes_key
        sess = self.e2ee_sessions.get(peer_username)
        peer_pub = pubkey_from_bytes(base64.b64decode(peer_pub_b64))
        salt = base64.b64decode(salt_b64)
        shared = sess["my_priv"].exchange(peer_pub)
        key = derive_aes_key(shared, salt, self.e2ee_info_for_peer(peer_username))
        sess.update({"peer_pub": peer_pub, "aes_key": key, "salt": salt})
        # send back our pub to finalize (if we are responder)
        my_pub_b64 = base64.b64encode(sess["my_pub"]).decode("utf-8")
        reply = {"target": peer_username, "pub": my_pub_b64, "salt": base64.b64encode(salt).decode("utf-8")}
        self.run_coroutine_threadsafe(self.send_json_to_server({"command": "KEY_REPLY", "payload": reply}))
        self.add_message_to_chatbox("SYS_MSG", f"🔐 {peer_username} ile E2E kuruldu.")

    def create_chat_ui(self):
            """Ana sohbet arayüzünü .grid() kullanarak oluşturur.
            (Sohbet Baloncukları ve Yazıyor... Etiketi DAHİL)"""

            # --- Ana Pencere Izgarasını Yapılandır ---
            self.grid_rowconfigure(0, weight=1)
            self.grid_columnconfigure(0, weight=1)

            # --- Ana Çerçeve ---
            self.main_chat_frame = ctk.CTkFrame(self)
            self.main_chat_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)


            #kendi kameram
            # --- main_chat_frame Izgarasını Yapılandır ---
            # 3 satır: 0 (sohbet/liste), 1 (yazıyor...), 2 (giriş)
            self.main_chat_frame.grid_rowconfigure(0, weight=1)  # Satır 0 (sohbet kutuları) genişlesin
            self.main_chat_frame.grid_rowconfigure(1, weight=0)  # Satır 1 (yazıyor) sabit
            self.main_chat_frame.grid_rowconfigure(2, weight=0)  # Satır 2 (mesaj girişi) sabit

            self.main_chat_frame.grid_columnconfigure(0, weight=3)  # Sütun 0 (sohbet)
            self.main_chat_frame.grid_columnconfigure(1, weight=1)  # Sütun 1 (online liste)

            # --- Bileşenleri Yerleştir ---

            # Sohbet Kutusu (Artık ScrollableFrame)
            self.chat_box = ctk.CTkScrollableFrame(self.main_chat_frame, fg_color="transparent")
            self.chat_box.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))
            self.chat_box.grid_columnconfigure(0, weight=1)

            # Çevrimiçi Kullanıcı Listesi
            self.online_users_frame = ctk.CTkScrollableFrame(self.main_chat_frame, width=150)
            self.online_users_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 5))
            self.online_users_frame.grid_columnconfigure(0, weight=1)

            # "Yazıyor..." Etiketi (row=1'e geri döndü)
            self.typing_status_label = ctk.CTkLabel(self.main_chat_frame, text="", height=20,
                                                    text_color="#AAAAAA", anchor="w")
            self.typing_status_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10)

            # Mesaj Giriş Kutusu (row=2'ye alındı)
            self.message_entry = ctk.CTkEntry(self.main_chat_frame, placeholder_text="/help yazarak komutları görün")
            self.message_entry.grid(row=2, column=0, sticky="ew", padx=(0, 5))
            self.record_button = ctk.CTkButton(self.main_chat_frame, text="🎤", width=40,
                                               command=self.toggle_voice_message)  # <-- YENİ
            self.record_button.grid(row=2, column=2, sticky="nsew", padx=(5, 0))
            #kendi kameram
            self.record_button.grid(row=2, column = 2, sticky = "nsew", padx = (5, 0))

            # --- YENİ EKLENTİ: Kamera Test Butonu ---
            self.camera_test_button = ctk.CTkButton(self.main_chat_frame, text="Kamera Test", width=80,
                                                    command=self.start_camera_preview_window)
            self.camera_test_button.grid(row=2, column=3, sticky="nsew", padx=(5, 0))
            # --- YENİ EKLENTİ SONU ---
            # Gönder Butonu (İkonlu) (row=2'ye alındı)
            self.send_button = ctk.CTkButton(self.main_chat_frame,
                                             image=self.send_icon, text="", width=40,
                                             command=self.send_chat_message)
            self.send_button.grid(row=2, column=1, sticky="nsew", padx=(5, 0))

            # Tuş Bağlantıları
            self.message_entry.bind("<Return>", self.send_chat_message)
            self.message_entry.bind("<KeyRelease>", self.on_key_press)

    async def preview_camera(self):
        cap = cv2.VideoCapture(0)
        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(img)
            tk_img = CTkImage(light_image=pil_img, size=(320, 240))
            self.video_label.configure(image=tk_img)
            self.video_label.image = tk_img
            await asyncio.sleep(0.03)  # ~30 FPS

    def on_key_press(self, event=None):
        """Kullanıcı mesaj kutusuna bir tuşa bastığında tetiklenir."""

        # 'Enter' tuşuna basıldıysa (bu send_chat_message'in işi) veya
        # '/quit' yazdıysak, 'START' komutu göndermeye gerek yok.
        if event and (event.keysym == 'Return' or self.message_entry.get().startswith('/')):
            return

        # 1. Eğer "Yazmıyor" durumundaysak, "Yazıyor" durumuna geç
        if not self._am_i_typing:
            self.run_coroutine_threadsafe(
                self.send_json_to_server({"command": "TYPING_START", "payload": {}})
            )
            self._am_i_typing = True

        # 2. Mevcut "Durdur" zamanlayıcısı varsa iptal et
        if self._typing_timer:
            self.after_cancel(self._typing_timer)

        # 3. "Durdur" komutunu göndermek için 3 saniyelik YENİ bir zamanlayıcı başlat
        self._typing_timer = self.after(3000, self.stop_typing_action)

    def stop_typing_action(self):
        """Sunucuya 'TYPING_STOP' gönderir ve durumu sıfırlar."""

        # 4. Zamanlayıcıyı sıfırla
        self._typing_timer = None

        # 5. Eğer "Yazıyor" durumundaysak, "Durdur" komutu gönder
        if self._am_i_typing:
            self.run_coroutine_threadsafe(
                self.send_json_to_server({"command": "TYPING_STOP", "payload": {}})
            )
            self._am_i_typing = False

    def update_typing_status(self, username, is_typing):
        """'Kimler yazıyor' listesini ve GUI etiketini günceller."""

        if is_typing:
            self.who_is_typing.add(username)  # Set'e ekle
        else:
            self.who_is_typing.discard(username)  # Set'ten çıkar

        # Arayüz etiketini güncelle
        label_text = ""
        typing_list = list(self.who_is_typing)  # Set'i listeye çevir

        if len(typing_list) == 1:
            label_text = f"{typing_list[0]} yazıyor..."
        elif len(typing_list) == 2:
            label_text = f"{typing_list[0]} ve {typing_list[1]} yazıyor..."
        elif len(typing_list) > 2:
            label_text = "Birkaç kişi yazıyor..."

        self.typing_status_label.configure(text=label_text)
    def load_history_messages(self, history_list):
            """Gelen sohbet geçmişi LİSTESİNİ sohbet kutusuna yükler."""
            try:
                # Geçmişin başına bir ayraç ekle
                self.add_message_to_chatbox("SYS_MSG", "--- Sohbet Geçmiși Yüklendi ---")

                # Gelen tüm geçmiş mesajlar için 'CHAT' komutunu taklit et
                # (Çünkü sunucu [Tarih - Kullanıcı]: Mesaj formatında gönderiyor,
                # bu da 'add_message_to_chatbox'un 'CHAT' parsing'i ile uyumlu)
                for msg in history_list:
                    if msg:  # Boş satırları atla
                        self.add_message_to_chatbox("CHAT", msg)

            except Exception as e:
                print(f"Sohbet geçmişi arayüze yüklenemedi: {e}")

            # --- YENİ v4.0 SESLİ MESAJ FONKSİYONLARI ---

    def toggle_voice_message(self):
        """'Sesli Mesaj' 🎤 butonuna basıldığında tetiklenir."""
        if self.is_recording:
            # 1. Kayıt Zaten Sürüyorsa: Kaydı Durdur
            self.is_recording = False
            self.record_button.configure(text="İşleniyor...", fg_color="#E67E22", state="disabled")
            # Kayıt thread'i 'self.is_recording = False' gördüğünde
            # otomatik olarak duracak ve 'process_and_upload_audio'yu tetikleyecek.
        else:
            # 2. Kaydı Başlat
            self.is_recording = True
            self.audio_frames = []  # Önceki kaydı temizle
            self.record_button.configure(text="🔴 Kayıt (Durdur)", fg_color="red",state="enabled")

            # Kaydı GUI'yi dondurmamak için ayrı bir 'daemon' thread'de başlat
            threading.Thread(target=self._record_audio_worker, daemon=True).start()



    def request_audio_file(self, file_id):
            """Sunucudan indirilmesi için bir ses dosyası talep eder."""
            print(f"DEBUG: Ses dosyası isteniyor: {file_id}")
            payload = {
                "command": "FETCH_AUDIO",
                "payload": {
                    "file_id": file_id
                }
            }
            self.run_coroutine_threadsafe(self.send_json_to_server(payload))



    def play_audio_chunk(self, audio_data_bytes):
        """Sunucudan gelen tam (sıkıştırılmış) ses dosyasını çözer ve çalar."""

        print("DEBUG (Player): Faz 1 - 'play_audio_chunk' tetiklendi.")

        # Sesi çalmak, ana arayüzü (GUI) dondurur.
        # Bu yüzden, sesi 'daemon' bir thread'de açıp çalmalıyız.
        def play_in_thread(audio_bytes):
            try:
                print("DEBUG (Player): Faz 2 (Thread) - Veri 'in-memory' dosyaya yükleniyor...")

                # --- DÜZELTME BURADA BAŞLIYOR ---
                # 1. Ham byte verisini 'dosya gibi' davranan bir hafıza objesine yükle

                audio_file = io.BytesIO(audio_bytes)

                print("DEBUG (Player): Faz 3 (Thread) - 'pydub' (ffmpeg) ile ses çözülüyor...")
                # 2. 'AudioSegment' yerine 'from_file' kullan
                #    ve 'format'ı burada belirt
                segment = pydub.AudioSegment.from_file(audio_file, format="mp3")
                # --- DÜZELTME SONU ---

                print(f"DEBUG (Player): Faz 4 (Thread) - Ses çözüldü! (Süre: {segment.duration_seconds:.1f}s)")

                # 3. 'sounddevice' ile çal
                sd.play(segment.get_array_of_samples(), segment.frame_rate)
                sd.wait()  # Çalma işlemi bitene kadar bekle
                print("DEBUG (Player): Faz 5 (Thread) - Oynatma bitti.")

            except Exception as e:
                print(f"--- SES ÇALMA THREAD HATASI ---")
                print(f"Hata: {e}")
                traceback.print_exc(file=sys.stderr)
                print(f"---------------------------------")
                self.schedule_gui_update(self.add_message_to_chatbox, "SYS_MSG_ERR", f"Ses dosyası oynatılamadı: {e}",
                                         None)

        # 'play_in_thread' fonksiyonunu yeni bir thread'de başlat
        print("DEBUG (Player): Oynatma için yeni thread başlatılıyor...")
        threading.Thread(target=play_in_thread, args=(audio_data_bytes,), daemon=True).start()

    def _record_audio_worker(self):
        """(Worker Thread) 'sounddevice' kullanarak sesi 'self.audio_frames' listesine kaydeder."""

        try:
            # 1. Kaydı başlat
            with sd.InputStream(samplerate=self.rate,
                                blocksize=self.chunk,
                                dtype=self.dtype,
                                channels=self.channels) as stream:

                # Maksimum 10 saniyelik kare (frame) sayısını hesapla
                max_frames = int((self.rate / self.chunk) * self.MAX_RECORD_SECONDS)

                for _ in range(max_frames):
                    # 2. Eğer kullanıcı butona tekrar basıp kaydı durdurduysa (is_recording=False)
                    # veya 10 saniye dolduysa, döngüden çık
                    if not self.is_recording:
                        break

                    data, overflowed = stream.read(self.chunk)
                    self.audio_frames.append(data)

            # 3. Kayıt bitti (ya 10sn doldu ya da kullanıcı durdurdu)
            print(f"Kayıt tamamlandı. {len(self.audio_frames)} parça yakalandı.")
            self.is_recording = False  # Durumu her ihtimale karşı sıfırla

            # 4. Sıkıştırma ve Yükleme işlemini 'asyncio' thread'ine devret
            # ('to_thread' kullanamayız, çünkü bu 'asyncio' thread'i değil,
            # 'threading' thread'i. O yüzden 'run_coroutine_threadsafe' kullanıyoruz)
            self.run_coroutine_threadsafe(self.process_and_upload_audio())


        except Exception as e:

            print(f"Mikrofon kayıt hatası: {e}")

            self.schedule_gui_update(self.add_message_to_chatbox, "SYS_MSG_ERR", f"Mikrofon hatası: {e}")

            # --- DÜZELTİLMİŞ SATIR ---

            self.schedule_gui_update(self.record_button.configure, text="🎤", fg_color="#3B8ED0", state="normal")

        # 'process_and_upload_audio' fonksiyonunu TAMAMEN bununla değiştir:

    async def process_and_upload_audio(self):
            """(Asyncio Thread) Kaydedilen sesi sıkıştırır, base64'e kodlar ve sunucuya gönderir."""

            print("DEBUG (Audio): Faz 1 - 'process_and_upload_audio' başladı.")
            try:
                self.schedule_gui_update(self.add_message_to_chatbox, "SYS_MSG", "Ses işleniyor ve sıkıştırılıyor...",
                                         None)

                if not self.audio_frames:
                    print("DEBUG (Audio): HATA - Ses karesi (frames) yok, işlem iptal edildi.")
                    return  # finally bloğu çalışır

                recording_data = np.concatenate(self.audio_frames)
                print(f"DEBUG (Audio): Faz 2 - Ses birleştirildi ({len(recording_data)} sample).")



                def convert_to_pydub(data):
                    # Bu 'sync' (donan) bir thread'de çalışır
                    print("DEBUG (Audio): Faz 3 (Thread) - 'pydub' dönüştürme başlıyor...")
                    segment = pydub.AudioSegment(
                        data=data.tobytes(),
                        sample_width=data.dtype.itemsize,
                        frame_rate=self.rate,
                        channels=self.channels
                    )

                    print(
                        "DEBUG (Audio): Faz 4 (Thread) - Sıkıştırma (export) başlıyor... (Eğer burada takılırsa, ffmpeg hatasıdır)")
                    segment.export("temp_audio.mp3", format="mp3", bitrate="32k")
                    print("DEBUG (Audio): Faz 5 (Thread) - Sıkıştırma bitti.")

                # 'pydub/ffmpeg' işlemini ayrı bir thread'e gönder
                await asyncio.to_thread(convert_to_pydub, recording_data)

                print("DEBUG (Audio): Faz 6 - Dosya okunuyor...")
                with open("temp_audio.mp3", "rb") as f:
                    audio_bytes = f.read()

                with open("temp_audio.mp3", "rb") as f:
                    audio_bytes = f.read()
                audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
                duration = len(recording_data) / self.rate
                #  Burada şifrele

                sess = self.e2ee_sessions.get("target")  # DM için hedef kullanıcı
                if sess and "aes_key" in sess:
                    from crypto_e2ee import seal
                    aad = f"audio:{self.nickname}:{target_user}".encode("utf-8")
                    nonce, ct = seal(sess["aes_key"], audio_bytes, aad=aad)

                    payload = {
                        "command": "AUDIO_MSG",
                        "payload": {
                            "filedata_b64": base64.b64encode(ct).decode("utf-8"),
                            "format": "mp3+gcm",
                            "duration_seconds": duration,
                            "nonce": base64.b64encode(nonce).decode("utf-8"),
                            "salt": base64.b64encode(sess["salt"]).decode("utf-8"),
                            "aad": base64.b64encode(aad).decode("utf-8"),
                            "target": target_user
                        }
                    }
                else:
                    # fallback: şifresiz gönder
                    payload = {
                        "command": "AUDIO_MSG",
                        "payload": {"filedata_b64": audio_base64, "format": "mp3", "duration_seconds": duration}
                    }





                os.remove("temp_audio.mp3")  # Geçici dosyayı sil
                print("DEBUG (Audio): Faz 7 - Base64 kodlandı, sunucuya gönderiliyor...")


                payload = {
                    "command": "AUDIO_MSG",
                    "payload": {"filedata_b64": audio_base64, "format": "opus", "duration_seconds": duration}
                }
                await self.send_json_to_server(payload)
                self.schedule_gui_update(self.add_message_to_chatbox, "SYS_MSG", "Sesli mesaj gönderildi.", None)
                print("DEBUG (Audio): Faz 8 - Başarıyla gönderildi.")



            except Exception as e:
                print(f"Ses işleme/yükleme hatası: {e}")
                traceback.print_exc(file=sys.stderr)
                self.schedule_gui_update(self.add_message_to_chatbox, "SYS_MSG_ERR", f"Ses işlenemedi: {e}", None)
            finally:
                # Bu, buton kilidini açan 'kurtarma' bloğudur
                print("DEBUG (Audio): Faz 9 (Finally) - Buton sıfırlanıyor.")
                self.schedule_gui_update(self.record_button.configure(text="🎤", fg_color="#3B8ED0", state="normal"))

    def update_online_list_ui(self, user_list):
        """Kullanıcı listesini (ScrollableFrame) tıklanabilir düğmelerle günceller."""
        try:
            # Önce mevcut tüm düğmeleri temizle
            for widget in self.online_users_frame.winfo_children():
                widget.destroy()

            # Başlık ekle
            title_label = ctk.CTkLabel(self.online_users_frame,
                                       text=f"Çevrimiçi ({len(user_list)}):",
                                       font=ctk.CTkFont(weight="bold"))
            title_label.grid(row=0, column=0, sticky="ew", padx=10, pady=(5, 10))

            # Listeyi (sözlükleri) döngüye al [cite: 101]
            row_index = 1
            for user_data in user_list:
                username = user_data.get("username", "Bilinmeyen")
                role = user_data.get("role", "user")

                display_name = ""
                if role == 'admin':
                    display_name = f"⭐ {username}"
                else:
                    display_name = f"{username}"

                # Tıklanabilir Düğme Oluştur
                user_button = ctk.CTkButton(
                    self.online_users_frame,
                    text=display_name,
                    fg_color="transparent",
                    hover_color="#3B8ED0",
                    anchor="w",
                    command=lambda u=username: self.open_private_chat(u)
                )
                user_button.grid(row=row_index, column=0, sticky="ew", padx=5)

                # Kendini (listede) devre dışı bırak
                if username == self.nickname:
                    user_button.configure(state="disabled", text=f"{display_name} (Siz)")

                row_index += 1

        except Exception as e:
            print(f"Online liste güncellenemedi: {e}")
            traceback.print_exc(file=sys.stderr)
            pass


    def send_chat_message(self, event=None):
            """Mesajı veya komutu JSON formatında sunucuya gönderir."""

            # 1. Her zaman "yazmayı durdur" komutunu tetikle
            self.stop_typing_action()

            message = self.message_entry.get()
            if not message:
                return

            payload_json = None  # Gönderilecek bir şey var mı diye kontrol için None ile başla

            # --- Komut Zinciri Başlangıcı ---

            # 1. Çıkış Komutları
            if message.lower() == '/quit' or message.lower() == '/exit':
                self.on_closing()
                return  # Fonksiyondan tamamen çık

            # 2. Yardım Komutu (Yerel)
            elif message.lower() == '/help':
                self.add_message_to_chatbox("SYS_MSG", "--- Komut Listesi ---")
                self.add_message_to_chatbox("SYS_MSG", " /dm <kullanici> <mesaj> - Özel mesaj gönderir.")
                self.add_message_to_chatbox("SYS_MSG", " /kick <kullanici> (Admin yetkisi gerekir)")
                self.add_message_to_chatbox("SYS_MSG", " /quit veya /exit - Sohbetten çıkar.")
                self.add_message_to_chatbox("SYS_MSG", " /help - Bu yardım menüsünü gösterir.")
                self.message_entry.delete(0, "end")
                return  # Fonksiyondan tamamen çık

            # 3. DM Komutu (Sunucuya Gönder)
            elif message.startswith('/dm '):
                parts = message.split(' ', 2)
                if len(parts) < 3:
                    self.add_message_to_chatbox("SYS_MSG_ERR", "Kullanım: /dm <kullanici> <mesaj>")
                    self.message_entry.delete(0, "end")
                    return  # Hatalı, fonksiyondan çık

                payload_json = {"command": "DM", "payload": {"target": parts[1], "message": parts[2]}}

            # 4. Kick Komutu (Sunucuya Gönder)
            elif message.startswith('/kick '):
                parts = message.split(' ', 1)
                if len(parts) < 2 or ' ' in parts[1] or not parts[1]:
                    self.add_message_to_chatbox("SYS_MSG_ERR", "Kullanım: /kick <kullanici_adi>")
                    self.message_entry.delete(0, "end")
                    return  # Hatalı, fonksiyondan çık

                target_user = parts[1]
                payload_json = {"command": "KICK", "payload": {"target": target_user}}

            # 5. Normal Sohbet Mesajı (Sunucuya Gönder)
            else:
                payload_json = {"command": "CHAT", "payload": {"message": message}}

            # --- Komut Zinciri Sonu ---

            # Eğer gönderilecek geçerli bir 'payload' varsa (yani /help veya /quit değilse)
            if payload_json:
                self.run_coroutine_threadsafe(self.send_json_to_server(payload_json))
                self.play_outgoing_sound()

            self.message_entry.delete(0, "end")

            # 'add_message_to_chatbox' fonksiyonunu TAMAMEN bununla değiştir:


    def add_message_to_chatbox(self, command, payload, sender=None):
        """Gelen JSON komutuna göre mesajı bir 'baloncuk' olarak oluşturur ve ekler."""

        message_type = "other"; text_color = "white"; bubble_color = "#2B2B2B"
        sticky_side = "w"; justify_text = "left"

        if not isinstance(payload, str): payload = str(payload)

        # --- 2. Özel Durum: Bu bir Sesli Mesaj mı? ---
        is_audio_message = False
        audio_file_id = None
        if command == "CHAT" and "[▶️ Sesli Mesaj" in payload:
            is_audio_message = True
            try:
                audio_file_id = payload.split(' - ID: ')[1].strip(']')
            except Exception as e:
                print(f"Sesli mesaj ID'si ayıklanamadı: {e}")
                is_audio_message = False

        # --- 3. Baloncuk Stillerini Ayarla (Mevcut kod) ---
        if command == "CHAT":
            try:
                sender_part = payload.split(' - ', 1)[1]
                sender = sender_part.split(']:', 1)[0]
                if sender == self.nickname: message_type = "own"
            except Exception: pass
        elif command == "DM":
            if payload.startswith("[Siz ->"): message_type = "own"
            bubble_color = "#88AAFF"; text_color = "black"
        elif command == "SYS_MSG":
            message_type = "system"; bubble_color = "transparent"
            text_color = "#AAAAAA"; sticky_side = "ew"; justify_text = "center"
        elif command == "SYS_MSG_ERR":
            message_type = "system"; bubble_color = "transparent"
            text_color = "#FF5555"; sticky_side = "ew"; justify_text = "center"

        if message_type == "own":
            bubble_color = "#3B8ED0"; sticky_side = "e"

        if is_audio_message and command != "DM":
             bubble_color = "#20639B" # Sesli mesaj için özel renk

        # --- 4. Baloncuğu Oluştur (DÜZELTİLMİŞ KISIM) ---
        try:
            bubble_wrapper = ctk.CTkFrame(self.chat_box, fg_color="transparent")
            bubble_wrapper.grid(sticky=sticky_side, padx=10, pady=2, column=0)

            # Eğer bu bir sesli mesaj ise, bir BUTON oluştur
            if is_audio_message and audio_file_id:
                display_text = payload.split(' - ID: ')[0] + "]"

                message_widget = ctk.CTkButton(bubble_wrapper,
                                             text=display_text,
                                             fg_color=bubble_color,
                                             text_color=text_color,
                                             corner_radius=10,
                                             # --- DÜZELTME: HATA BURADAYDI, KALDIRILDI ---
                                             # justify=justify_text,
                                             # --- DÜZELTME SONU ---
                                             command=lambda file_id=audio_file_id: self.request_audio_file(file_id))
            else:
                # Normal metin mesajı ise, bir ETİKET oluştur
                message_widget = ctk.CTkLabel(bubble_wrapper,
                                             text=payload,
                                             fg_color=bubble_color,
                                             text_color=text_color,
                                             corner_radius=10,
                                             wraplength=400,
                                             justify=justify_text, # <- Buradaki 'justify' doğru ve kalmalı
                                             padx=10, pady=5)

            message_widget.grid()

            # 5. Sesi Çal (Gelen mesaj sesi)
            if not message_type == "own":
                 self.play_incoming_sound()

            # 6. En alta kaydır
            self.after(100, self.chat_box._parent_canvas.yview_moveto, 1.0)

        except Exception as e:
            print(f"Baloncuk oluşturma hatası: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        # ChatApp sınıfının içine, diğer def fonksiyonlarıyla aynı hizaya EKLEYİN:

    def set_auth_buttons_state(self, state):
            """Giriş ve Kayıt butonlarının durumunu ayarlar ('normal' veya 'disable')."""
            try:
                if state == "disable":
                    if hasattr(self, 'login_button'):  # Butonun varlığını kontrol et
                        self.login_button.configure(state=state)
                    if hasattr(self, 'register_button'):
                        self.register_button.configure(state="normal")
                else:
                    if hasattr(self, 'login_button'):
                        self.login_button.configure(state="normal")
                    if hasattr(self, 'register_button'):
                        self.register_button.configure(state="normal")
            except (AttributeError, tkinter.TclError):
                # Butonlar henüz oluşturulmadıysa (nadiren olur) görmezden gel
                pass

    def clear_widgets(self):
        """Penceredeki tüm bileşenleri (widget) temizler."""
        # .grid() ile yerleştirilen widget'ları temizlemenin en iyi yolu
        # .winfo_children() kullanmaktır, ancak ana pencere ızgarasını da sıfırlamalıyız

        # Önce tüm alt widget'ları yok et
        for widget in self.winfo_children():
            widget.destroy()

        # Ana pencerenin ızgara yapılandırmasını sıfırla
        # (Bu, yeni 'create' fonksiyonunun kendi ızgarasını kurabilmesi için önemlidir)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=0)
        self.grid_columnconfigure(2, weight=0)

    def go_back_to_login(self, error_message):
        """Bağlantı koptuğunda arayüzü sohbetten girişe döndürür."""
        if not self.authenticated:
            # Zaten giriş ekranındayken bağlantı koptuysa...
            self.show_auth_error(error_message)
            return

        # Sohbet ekranındayken bağlantı koptuysa...
        self.authenticated = False
        self.nickname = ""
        self.create_auth_ui()  # Giriş arayüzünü yeniden kur
        self.show_auth_error(error_message)  # Ve hatayı göster

    def play_incoming_sound(self):
        """Mevcut bir *gelen* ses zamanlayıcısı varsa iptal eder ve yenisini başlatır."""
        if self._sound_cooldown_timer_in:
            self.after_cancel(self._sound_cooldown_timer_in)
        # Düzeltme burada (tek 'actually' ve fonksiyon adının başındaki '_' (alt tire)):
        self._sound_cooldown_timer_in = self.after(300, self._actually_play_incoming)
    def _actually_play_incoming(self):
            """Zamanlayıcı bittiğinde *gelen* sesi çalar."""
            try:

                winsound.PlaySound(resource_path("assets/message.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as e:
                pass
            finally:
                self._sound_cooldown_timer_in = None

    def play_outgoing_sound(self):
        """Mevcut bir *giden* ses zamanlayıcısı varsa iptal eder ve yenisini başlatır."""
        if self._sound_cooldown_timer_out:
            self.after_cancel(self._sound_cooldown_timer_out)
        self._sound_cooldown_timer_out = self.after(300, self._actually_play_outgoing)

    def _actually_play_outgoing(self):
        """Zamanlayıcı bittiğinde *giden* sesi çalar."""
        try:
            # Giden ses dosyasının 'assets' klasöründe olduğunu varsayıyorum
            winsound.PlaySound(resource_path("assets/message.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            print(f"Giden ses dosyası ('assets/message.wav') bulunamadı: {e}")
            pass
        finally:
            self._sound_cooldown_timer_out = None



    # 'on_closing' fonksiyonunun HEMEN ÜZERİNE (sınıfın bir metodu olarak) ekleyin:

    def start_camera_preview_window(self):
        """Kamera testi için yeni bir pencere açar."""

        # Zaten bir test penceresi açık mı?
        if hasattr(self, "camera_preview_window") and self.camera_preview_window.winfo_exists():
            self.camera_preview_window.lift()  # Pencereyi öne getir
            return

        # Yeni Toplevel penceresi oluştur
        self.camera_preview_window = ctk.CTkToplevel(self)
        self.camera_preview_window.title("Kamera Testi (Lokal Önizleme)")
        self.camera_preview_window.geometry("640x480")

        # Video görüntüsünün gösterileceği etiketi oluştur
        self.camera_preview_label = ctk.CTkLabel(self.camera_preview_window, text="Kamera bağlanıyor...")
        self.camera_preview_label.pack(fill="both", expand=True)

        # Kamera akışını (coroutine) güvenli bir şekilde başlat
        self.camera_preview_task = self.run_coroutine_threadsafe(
            self.run_local_camera_feed(self.camera_preview_label)
        )

        # Pencere kapatıldığında coroutine'i durdurmak için protokol ata
        self.camera_preview_window.protocol(
            "WM_DELETE_WINDOW", self.stop_camera_preview_window
        )

    def stop_camera_preview_window(self):
        """Kamera test penceresini ve kamera akışını güvenle durdurur."""

        # 1. Arka planda çalışan kamera coroutine'ini iptal et
        if hasattr(self, "camera_preview_task"):
            try:
                # 'run_coroutine_threadsafe' bir 'future' nesnesi döndürür
                # Bu 'future' üzerinden 'cancel()' çağrılabilir
                self.camera_preview_task.cancel()
            except Exception as e:
                print(f"Kamera görevini iptal etme hatası: {e}")

        # 2. Pencereyi yok et
        if hasattr(self, "camera_preview_window") and self.camera_preview_window.winfo_exists():
            self.camera_preview_window.destroy()

        # 3. Referansları temizle
        if hasattr(self, "camera_preview_window"):
            del self.camera_preview_window
        if hasattr(self, "camera_preview_label"):
            del self.camera_preview_label
        if hasattr(self, "camera_preview_task"):
            del self.camera_preview_task

    async def run_local_camera_feed(self, video_label):
        """Lokal kamerayı açar ve sağlanan CTkLabel'a yansıtır."""
        cap = None
        try:
            cap = cv2.VideoCapture(0)  # 0, varsayılan kameradır
            if not cap.isOpened():
                print("HATA: Kamera (index 0) açılamadı!")
                self.schedule_gui_update(video_label.configure, text="Hata: Kamera açılamadı.")
                return

            while True:
                # --- YENİ GÜVENLİK KONTROLÜ ---
                # Döngünün başında, 'video_label' hala var mı diye kontrol et.
                # Eğer pencere kapatıldıysa, bu 'False' döner ve döngü temizce durur.
                try:
                    if not video_label.winfo_exists():
                        break
                except Exception:
                    # (video_label'ın kendisi None olduysa vb. nadir durumlar için)
                    break
                # --- KONTROL SONU ---
                ret, frame = cap.read()
                if not ret:
                    break

                # Görüntüyü GUI'de göstermek için hazırla (OpenCV BGR -> RGB)
                img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(img)

                # Pencere boyutu değişebileceği için label'ın o anki boyutunu al
                w = video_label.winfo_width()
                h = video_label.winfo_height()

                # Sadece geçerli boyutlar varsa (pencere küçültülmemişse)
                if w > 10 and h > 10:
                    # Görüntüyü label'a sığacak şekilde yeniden boyutlandır (oranı koru)
                    pil_img.thumbnail((w, h), Image.LANCZOS)
                    tk_img = CTkImage(light_image=pil_img, size=pil_img.size)

                    # GUI'yi ana thread'de güncelle (schedule_gui_update ile)
                    def update_gui_label(img_to_set=tk_img):
                        try:
                            # 'try-except' bloğu, pencere aniden kapatılırsa oluşacak hataları yakalar
                            video_label.configure(image=img_to_set, text="")
                            video_label.image = img_to_set  # Referansı sakla (çöp toplayıcı silmesin)
                        except Exception:
                            pass

                    self.schedule_gui_update(update_gui_label)

                await asyncio.sleep(0.03)  # ~30 FPS

        except asyncio.CancelledError:
            print("Kamera önizlemesi (lokal) durduruldu.")
        except Exception as e:
            print(f"Kamera önizleme hatası: {e}")
            traceback.print_exc(file=sys.stderr)
        finally:
            # Temizlik: Kamera kaynağını serbest bırak
            if cap:
                cap.release()

            # Label'ı temizle
            def clear_gui_label():
                try:
                    video_label.configure(image=None, text="Kamera Kapatıldı.")
                    video_label.image = None
                except Exception:
                    pass

            self.schedule_gui_update(clear_gui_label)





        # 'on_closing' fonksiyonunun HEMEN ÜZERİNE (sınıfın bir metodu olarak) ekleyin:
    async def shutdown_async_tasks(self):
            """Asyncio görevlerini (websocket) güvenle kapatır ve loop'u durdurur."""
            print("DEBUG (Async): Kapatma coroutine'i başladı...")
            try:
                if self.websocket:
                    await self.websocket.close()
                    print("DEBUG (Async): WebSocket kapatıldı.")
            except Exception as e:
                print(f"DEBUG (Async): WebSocket kapatılırken hata: {e}")
            finally:
                print("DEBUG (Async): Event loop durduruluyor.")
                if self.asyncio_loop.is_running():
                    self.asyncio_loop.stop()

        # Mevcut 'on_closing' fonksiyonunuzu BUNUNLA DEĞİŞTİRİN:
    def on_closing(self):
            """Pencere kapatıldığında tetiklenir."""
            print("DEBUG (Main): Kapatma isteği gönderildi...")

            # Hata ayıklama: shutdown_async_tasks'in var olup olmadığını kontrol et
            if not hasattr(self, 'shutdown_async_tasks'):
                print("KRİTİK HATA: shutdown_async_tasks fonksiyonu bulunamadı!")
                self.destroy()  # Kaba kuvvetle kapat
                return

            if self.websocket or self.asyncio_loop.is_running():
                # Arka plan thread'ine 'kendini nazikçe kapat' görevini ver
                self.run_coroutine_threadsafe(self.shutdown_async_tasks())

            # Pencereyi hemen yok et (kullanıcı beklemesin)
            self.destroy()


if __name__ == "__main__":
    app = ChatApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
