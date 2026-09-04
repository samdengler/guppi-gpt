// The page's privacy notice, in one place because two switches decide what it may claim.
// history would keep chats in browser storage; logging records threads in the conversation
// log bucket. Both are off until the feature behind them ships.

const KEYS = "Enter to send, Shift+Enter for a new line.";

export function noticeSentence(history, logging) {
  if (history && logging) return "Chats are saved on this device and logged for troubleshooting.";
  if (history) return "Chats are saved on this device.";
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
