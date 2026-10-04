// amazon-connect-chatjs as its own ES module bundle, loaded only by a page whose project
// uses the Connect chat transport (web/src/connect-chat.js), so the other pages do not
// carry its 300 KB. The library sets window.connect when it runs; this module hands back
// its ChatSession object and keeps no reference of its own.
import "amazon-connect-chatjs";

export const ChatSession = globalThis.connect.ChatSession;
