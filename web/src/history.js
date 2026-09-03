// Local chat history: a thin wrapper around IndexedDB, built on the idb package. No
// server call, nothing sent anywhere. Stores thread text only, never tokens.
//
// Schema: one object store "threads", keyed by "id", each record
//   { id, title, createdAt, updatedAt, messages: [{ id, role, content, feedback? }] }
// `feedback`, on an assistant message only, is "up", "down", or null; set by
// setMessageFeedback below when the feedback control (web/src/feedback.js) is on.

import { openDB } from "idb";

const DB_NAME = "guppigpt-history";
const DB_VERSION = 1;
const STORE = "threads";

let dbPromise = null;

function openDb() {
  if (!dbPromise) {
    if (!("indexedDB" in globalThis)) {
      dbPromise = Promise.reject(new Error("indexedDB is not available"));
    } else {
      dbPromise = openDB(DB_NAME, DB_VERSION, {
        upgrade(db) {
          if (!db.objectStoreNames.contains(STORE)) {
            db.createObjectStore(STORE, { keyPath: "id" });
          }
        },
      });
    }
  }
  return dbPromise;
}

// Replaces (or creates) one thread record.
export async function putThread(thread) {
  const db = await openDb();
  await db.put(STORE, thread);
}

// Removes one thread record by id.
export async function deleteThread(id) {
  const db = await openDb();
  await db.delete(STORE, id);
}

// Removes every thread record.
export async function clearAll() {
  const db = await openDb();
  await db.clear(STORE);
}

// All threads, newest first.
export async function listThreads() {
  const db = await openDb();
  const all = await db.getAll(STORE);
  return all.sort((a, b) => b.updatedAt - a.updatedAt);
}

// The most recently updated thread, or null when the store is empty.
export async function newestThread() {
  const threads = await listThreads();
  return threads[0] || null;
}

// Sets (or, with vote null, clears) the feedback field on one message of one stored
// thread. A no-op when the thread has no stored record, so a vote on a reply from a
// thread that was never persisted does not create a partial one.
export async function setMessageFeedback(threadId, messageId, vote) {
  const db = await openDb();
  const thread = await db.get(STORE, threadId);
  if (!thread) return;
  thread.messages = thread.messages.map((message) =>
    message.id === messageId ? { ...message, feedback: vote } : message,
  );
  await db.put(STORE, thread);
}
