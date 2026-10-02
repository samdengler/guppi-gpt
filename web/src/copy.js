// The page's privacy notice, in one place because two switches decide what it may claim.
// history would keep chats in browser storage; logging records threads in the conversation
// log bucket. Both are off until the feature behind them ships.

const KEYS = "Enter to send, Shift+Enter for a new line.";

export function noticeSentence(history, logging) {
  if (history && logging) return "Chats are saved on this device and logged for troubleshooting.";
  if (history) return "Chats are saved on this device only.";
  if (logging) return "Conversations are logged for troubleshooting.";
  return null;
}

export function hintText(history, logging) {
  return `${KEYS} ${noticeSentence(history, logging) ?? "Nothing is saved."}`;
}

export function emptyStateText(history, logging) {
  const sentence = noticeSentence(history, logging);
  return sentence ? `Ask anything. ${sentence}` : "Ask anything. This conversation is not saved.";
}

// The status line while a tool runs and after it ends, chosen by the tool's name. The
// knowledge base search keeps its own wording; any other tool is named after its gateway
// target prefix (`<target>___`) is removed, with underscores read as spaces. An
// extension's guppi.status(text) still replaces either line.
const KNOWLEDGE_BASE_PREFIX = "docs___";

export function toolDisplayName(name) {
  const text = String(name || "");
  const bare = text.includes("___") ? text.slice(text.indexOf("___") + 3) : text;
  return bare.replace(/_+/g, " ").trim() || "a tool";
}

export function toolStatus(name, done) {
  if (String(name || "").startsWith(KNOWLEDGE_BASE_PREFIX)) {
    return done ? "Searched the knowledge base" : "Searching the knowledge base…";
  }
  const label = toolDisplayName(name);
  return done ? `Used ${label}` : `Using ${label}…`;
}
