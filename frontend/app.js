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
let authRequired = true;
let requestInFlight = false;

let mediaRecorder = null;
let mediaStream = null;
let recordingChunks = [];
let isRecording = false;
let isProcessingVoice = false;
let touchBlockMouseUntil = 0;

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

  const candidates = [
    "audio/webm;codecs=opus",
    "audio/mp4",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];

  for (const type of candidates) {
    if (window.MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return "";
}

function extensionFromMimeType(type) {
  if (!type) return "webm";
  if (type.includes("mp4") || type.includes("m4a")) return "m4a";
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
      appendMessage("assistant", "I couldn't process that recording. Please try again.");
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

async function startRecording() {
  if (isRecording || isProcessingVoice || requestInFlight) return;

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
      const fallbackType = recordingChunks[0]?.type || mimeType || "audio/webm";
      const blob = new Blob(recordingChunks, { type: fallbackType });
      clearRecorderState();
      await handleRecordingComplete(blob);
    };

    mediaRecorder.start();
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
  clearRecorderState();
  setVoiceVisualState();
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
}

function setPublicDemoState() {
  authRequired = false;
  loginPanel.classList.add("hidden");
  chatPanel.classList.remove("hidden");
  userBadge.textContent = "Public demo mode";
  logoutButton.classList.add("hidden");
  if (!chatMessages.childElementCount) {
    appendMessage("assistant", "Hi. What can I help you figure out?");
  }
  setVoiceVisualState();
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
  if (!authRequired) return;
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

async function bootstrap() {
  try {
    const health = await fetch("/health");
    if (health.ok) {
      const h = await health.json();
      authRequired = Boolean(h.auth_required);
      if (!authRequired) {
        setPublicDemoState();
        return;
      }
    }
  } catch {
    // ignore health probe failures and continue with auth flow
  }

  if (!accessToken && authRequired) {
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
