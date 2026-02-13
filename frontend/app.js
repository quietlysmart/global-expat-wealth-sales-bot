const chatForm = document.getElementById("chatForm");
const userMessageInput = document.getElementById("userMessage");
const chatMessages = document.getElementById("chatMessages");
const sendButton = chatForm.querySelector("button");

let conversationId = null;

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

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = userMessageInput.value.trim();
  if (!message) return;

  appendMessage("user", message);
  userMessageInput.value = "";
  userMessageInput.disabled = true;
  sendButton.disabled = true;
  const thinkingEl = showThinkingBubble();

  try {
    const payload = {
      message,
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      removeThinkingBubble(thinkingEl);
      appendMessage("assistant", "I hit a temporary issue. Please try again.");
      return;
    }

    const data = await res.json();
    conversationId = data.conversation_id;
    removeThinkingBubble(thinkingEl);
    appendMessage("assistant", data.assistant_reply);
  } catch (err) {
    removeThinkingBubble(thinkingEl);
    appendMessage("assistant", "I couldn't reach the backend right now.");
  } finally {
    userMessageInput.disabled = false;
    sendButton.disabled = false;
    userMessageInput.focus();
  }
});

appendMessage(
  "assistant",
  "Hi. What can I help you figure out?"
);
