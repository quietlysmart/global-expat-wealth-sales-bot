const loginPanel = document.getElementById("loginPanel");
const chatPanel = document.getElementById("chatPanel");
const loginForm = document.getElementById("loginForm");
const loginEmailInput = document.getElementById("loginEmail");
const loginPasswordInput = document.getElementById("loginPassword");
const loginError = document.getElementById("loginError");

const chatForm = document.getElementById("chatForm");
const userMessageInput = document.getElementById("userMessage");
const chatMessages = document.getElementById("chatMessages");
const sendButton = chatForm.querySelector("button[type='submit']");
const voiceRecordButton = document.getElementById("voiceRecordButton");
const logoutButton = document.getElementById("logoutButton");
const userBadge = document.getElementById("userBadge");

const TOKEN_KEY = "gew_sales_auth_token";
const EMAIL_KEY = "gew_sales_user_email";

let conversationId = null;
let accessToken = localStorage.getItem(TOKEN_KEY) || "";
let currentEmail = localStorage.getItem(EMAIL_KEY) || "";
const authRequired = true;
let requestInFlight = false;

let mediaRecorder = null;
let mediaStream = null;
let recordingChunks = [];
let isRecording = false;
let isProcessingVoice = false;
let touchBlockMouseUntil = 0;

const isIOS =
  /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
// Keep microphone behavior consistent everywhere: press/hold and release to send.
const supportsSpeechRecognition = false && Boolean(SpeechRecognitionCtor) && window.isSecureContext && !isIOS;
let speechRecognition = null;
let speechSessionActive = false;
let speechFinalText = "";
let speechInterimText = "";
let speechErrorMessage = "";

function scrollPageToTop() {
  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }
  window.scrollTo(0, 0);
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
}

function appendMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function showThinkingBubble() {
  const el = document.createElement("div");
  el.className = "msg assistant thinking";
  el.innerHTML = `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return el;
}

function removeThinkingBubble(el) {
  if (el && el.parentNode) {
    el.parentNode.removeChild(el);
  }
}

function setComposerDisabled(disabled) {
  requestInFlight = disabled;
  userMessageInput.disabled = disabled;
  sendButton.disabled = disabled;
  voiceRecordButton.disabled = disabled || isProcessingVoice;
}

function setVoiceVisualState() {
  voiceRecordButton.classList.toggle("recording", isRecording);
  voiceRecordButton.classList.toggle("processing", isProcessingVoice);
  voiceRecordButton.disabled = requestInFlight || isProcessingVoice;

  if (isRecording) {
    voiceRecordButton.title = "Release to stop";
    voiceRecordButton.setAttribute("aria-label", "Recording. Release to stop");
  } else if (isProcessingVoice) {
    voiceRecordButton.title = "Processing audio";
    voiceRecordButton.setAttribute("aria-label", "Processing recorded audio");
  } else {
    voiceRecordButton.title = "Hold to record";
    voiceRecordButton.setAttribute("aria-label", "Hold to record voice message");
  }
}

function clearRecorderState() {
  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
  }
  mediaStream = null;
  mediaRecorder = null;
  recordingChunks = [];
}

function pickBestMimeType() {
  if (!window.MediaRecorder || typeof window.MediaRecorder.isTypeSupported !== "function") {
    return "";
  }

  const candidates = isIOS
    ? ["audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"]
    : ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];

  for (const type of candidates) {
    if (window.MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return "";
}

function extensionFromMimeType(type) {
  if (!type) return isIOS ? "m4a" : "webm";
  if (type.includes("video/mp4")) return "mp4";
  if (type.includes("audio/mp4") || type.includes("m4a") || type.includes("x-m4a") || type.includes("aac")) return "m4a";
  if (type.includes("ogg")) return "ogg";
  if (type.includes("wav")) return "wav";
  return "webm";
}

function shouldIgnoreMouseEvent() {
  return Date.now() < touchBlockMouseUntil;
}

async function sendChatMessage(message) {
  const trimmed = (message || "").trim();
  if (!trimmed) return false;
  if (authRequired && !accessToken) return false;

  appendMessage("user", trimmed);
  setComposerDisabled(true);
  const thinkingEl = showThinkingBubble();

  try {
    const payload = {
      message: trimmed,
      conversation_id: conversationId,
      channel_hint: "demo_web",
      user_metadata: {
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        locale: navigator.language,
      },
      demo_mode: true,
    };

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: JSON.stringify(payload),
    });

    if (res.status === 401) {
      removeThinkingBubble(thinkingEl);
      appendMessage("assistant", "Your session expired. Please log in again.");
      setLoggedOutState();
      return false;
    }

    if (!res.ok) {
      removeThinkingBubble(thinkingEl);
      appendMessage("assistant", "I hit a temporary issue. Please try again.");
      return false;
    }

    const data = await res.json();
    conversationId = data.conversation_id;
    removeThinkingBubble(thinkingEl);
    appendMessage("assistant", data.assistant_reply);
    return true;
  } catch {
    removeThinkingBubble(thinkingEl);
    appendMessage("assistant", "I couldn't reach the backend right now.");
    return false;
  } finally {
    setComposerDisabled(false);
    setVoiceVisualState();
    userMessageInput.focus();
  }
}

async function handleRecordingComplete(blob) {
  if (!blob || blob.size === 0) {
    appendMessage("assistant", "I couldn't hear anything. Please try again.");
    return;
  }
  if (authRequired && !accessToken) {
    appendMessage("assistant", "Please log in before using voice input.");
    return;
  }

  isProcessingVoice = true;
  setVoiceVisualState();

  try {
    const ext = extensionFromMimeType(blob.type);
    const formData = new FormData();
    formData.append("audio", blob, `voice-input.${ext}`);

    const response = await fetch("/api/transcribe", {
      method: "POST",
      headers: {
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: formData,
    });

    if (response.status === 401) {
      appendMessage("assistant", "Your session expired. Please log in again.");
      setLoggedOutState();
      return;
    }

    if (!response.ok) {
      appendMessage(
        "assistant",
        "Voice transcription is temporarily unavailable. Please type your message."
      );
      return;
    }

    const data = await response.json();
    const transcript = (data.text || "").trim();
    if (!transcript) {
      appendMessage("assistant", "I couldn't catch that. Please try recording again.");
      return;
    }

    userMessageInput.value = "";
    await sendChatMessage(transcript);
  } catch {
    appendMessage("assistant", "Voice input is temporarily unavailable. Please type your message.");
  } finally {
    isProcessingVoice = false;
    setVoiceVisualState();
  }
}

function initSpeechRecognition() {
  if (!supportsSpeechRecognition || speechRecognition) return;

  speechRecognition = new SpeechRecognitionCtor();
  speechRecognition.lang = navigator.language || "en-US";
  speechRecognition.interimResults = true;
  speechRecognition.continuous = true;

  speechRecognition.onresult = (event) => {
    let finalText = speechFinalText;
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const segment = event.results[i][0]?.transcript || "";
      if (event.results[i].isFinal) {
        finalText += `${segment} `;
      } else {
        interim += segment;
      }
    }
    speechFinalText = finalText.trim();
    speechInterimText = interim.trim();
  };

  speechRecognition.onerror = (event) => {
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      speechErrorMessage = "I couldn't access your microphone. Please check permissions and try again.";
      return;
    }
    if (event.error === "no-speech" || event.error === "audio-capture") {
      speechErrorMessage = "I couldn't hear anything. Please try again.";
      return;
    }
    speechErrorMessage = "Voice input is temporarily unavailable. Please type your message.";
  };

  speechRecognition.onend = () => {
    if (!speechSessionActive) return;
    speechSessionActive = false;

    const transcript = `${speechFinalText} ${speechInterimText}`.trim();
    const err = speechErrorMessage;

    speechFinalText = "";
    speechInterimText = "";
    speechErrorMessage = "";

    if (err) {
      appendMessage("assistant", err);
      return;
    }
    if (!transcript) {
      appendMessage("assistant", "I couldn't catch that. Please try recording again.");
      return;
    }

    void sendChatMessage(transcript);
  };
}

async function startSpeechRecognitionRecording() {
  initSpeechRecognition();
  if (!speechRecognition) return false;

  speechSessionActive = true;
  speechFinalText = "";
  speechInterimText = "";
  speechErrorMessage = "";

  try {
    speechRecognition.start();
    isRecording = true;
    setVoiceVisualState();
    return true;
  } catch {
    speechSessionActive = false;
    return false;
  }
}

async function startRecording() {
  if (isRecording || isProcessingVoice || requestInFlight) return;

  if (supportsSpeechRecognition) {
    const speechStarted = await startSpeechRecognitionRecording();
    if (speechStarted) {
      return;
    }
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    appendMessage("assistant", "Voice recording isn't supported on this browser.");
    return;
  }

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = pickBestMimeType();
    const recorderOptions = mimeType ? { mimeType } : undefined;

    mediaRecorder = new MediaRecorder(mediaStream, recorderOptions);
    recordingChunks = [];

    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        recordingChunks.push(event.data);
      }
    };

    mediaRecorder.onstop = async () => {
      const fallbackType =
        mediaRecorder?.mimeType ||
        recordingChunks[0]?.type ||
        mimeType ||
        (isIOS ? "audio/mp4" : "audio/webm");
      const blob = new Blob(recordingChunks, { type: fallbackType });
      clearRecorderState();
      await handleRecordingComplete(blob);
    };

    mediaRecorder.start(250);
    isRecording = true;
    setVoiceVisualState();
  } catch {
    clearRecorderState();
    appendMessage("assistant", "I couldn't access your microphone. Please check permissions and try again.");
  }
}

function stopRecording() {
  if (!isRecording) return;

  isRecording = false;
  setVoiceVisualState();

  if (speechRecognition && speechSessionActive) {
    try {
      speechRecognition.stop();
    } catch {
      speechSessionActive = false;
    }
    return;
  }

  if (!mediaRecorder || mediaRecorder.state === "inactive") {
    clearRecorderState();
    return;
  }

  mediaRecorder.stop();
}

function setLoggedOutState() {
  accessToken = "";
  currentEmail = "";
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EMAIL_KEY);
  loginPanel.classList.remove("hidden");
  chatPanel.classList.add("hidden");
  chatMessages.innerHTML = "";
  conversationId = null;
  loginPasswordInput.value = "";
  isRecording = false;
  isProcessingVoice = false;
  speechSessionActive = false;
  speechFinalText = "";
  speechInterimText = "";
  speechErrorMessage = "";
  if (speechRecognition) {
    try {
      speechRecognition.stop();
    } catch {
      // ignore
    }
  }
  clearRecorderState();
  setVoiceVisualState();
  scrollPageToTop();
}

function setLoggedInState(email) {
  currentEmail = email || "";
  loginPanel.classList.add("hidden");
  chatPanel.classList.remove("hidden");
  userBadge.textContent = currentEmail;
  if (!chatMessages.childElementCount) {
    appendMessage("assistant", "Hi. What can I help you figure out?");
  }
  setVoiceVisualState();
  scrollPageToTop();
}

async function fetchMe() {
  if (!accessToken) return false;
  try {
    const res = await fetch("/api/me", {
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    });
    if (!res.ok) return false;
    const data = await res.json();
    currentEmail = data.email || currentEmail;
    localStorage.setItem(EMAIL_KEY, currentEmail);
    return true;
  } catch {
    return false;
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";

  const email = loginEmailInput.value.trim().toLowerCase();
  const password = loginPasswordInput.value;
  if (!email || !password) return;

  loginEmailInput.disabled = true;
  loginPasswordInput.disabled = true;

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    if (!res.ok) {
      loginError.textContent = "Invalid login details. Please try again.";
      return;
    }

    const data = await res.json();
    accessToken = data.access_token || "";
    currentEmail = data.user_email || email;
    localStorage.setItem(TOKEN_KEY, accessToken);
    localStorage.setItem(EMAIL_KEY, currentEmail);
    setLoggedInState(currentEmail);
  } catch {
    loginError.textContent = "Could not reach the server. Please try again.";
  } finally {
    loginEmailInput.disabled = false;
    loginPasswordInput.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  if (accessToken) {
    try {
      await fetch("/api/logout", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${accessToken}`,
        },
      });
    } catch {
      // ignore network logout failures in demo
    }
  }
  setLoggedOutState();
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = userMessageInput.value.trim();
  if (!message) return;

  userMessageInput.value = "";
  await sendChatMessage(message);
});

voiceRecordButton.addEventListener("mousedown", (event) => {
  if (event.button !== 0 || shouldIgnoreMouseEvent()) return;
  event.preventDefault();
  startRecording();
});

voiceRecordButton.addEventListener("mouseup", (event) => {
  if (event.button !== 0 || shouldIgnoreMouseEvent()) return;
  event.preventDefault();
  stopRecording();
});

voiceRecordButton.addEventListener("mouseleave", () => {
  if (shouldIgnoreMouseEvent()) return;
  stopRecording();
});

voiceRecordButton.addEventListener(
  "touchstart",
  (event) => {
    touchBlockMouseUntil = Date.now() + 800;
    event.preventDefault();
    startRecording();
  },
  { passive: false }
);

voiceRecordButton.addEventListener(
  "touchend",
  (event) => {
    touchBlockMouseUntil = Date.now() + 800;
    event.preventDefault();
    stopRecording();
  },
  { passive: false }
);

voiceRecordButton.addEventListener(
  "touchcancel",
  (event) => {
    touchBlockMouseUntil = Date.now() + 800;
    event.preventDefault();
    stopRecording();
  },
  { passive: false }
);

voiceRecordButton.addEventListener("contextmenu", (event) => {
  event.preventDefault();
});

voiceRecordButton.addEventListener("click", (event) => {
  event.preventDefault();
});

async function bootstrap() {
  scrollPageToTop();
  if (!accessToken) {
    setLoggedOutState();
    return;
  }
  const ok = await fetchMe();
  if (!ok) {
    setLoggedOutState();
    return;
  }
  setLoggedInState(currentEmail);
}

bootstrap();
