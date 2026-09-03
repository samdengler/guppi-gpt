// Local chat history: a small promise wrapper around IndexedDB. No library, no server
// call, nothing sent anywhere. Stores thread text only, never tokens.
//
// Schema: one object store "threads", keyed by "id", each record
//   { id, title, createdAt, updatedAt, messages: [{ id, role, content }] }

const DB_NAME = "guppigpt-history";
const DB_VERSION = 1;
const STORE = "threads";

let dbPromise = null;

function openDb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      if (!("indexedDB" in globalThis)) {
        reject(new Error("indexedDB is not available"));
        return;
      }
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  return dbPromise;
}

function wrap(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function store(mode) {
  const db = await openDb();
  return db.transaction(STORE, mode).objectStore(STORE);
}

// Replaces (or creates) one thread record.
export async function putThread(thread) {
  const objectStore = await store("readwrite");
  await wrap(objectStore.put(thread));
}

// Removes one thread record by id.
export async function deleteThread(id) {
  const objectStore = await store("readwrite");
  await wrap(objectStore.delete(id));
}

// Removes every thread record.
export async function clearAll() {
  const objectStore = await store("readwrite");
  await wrap(objectStore.clear());
}

// All threads, newest first.
export async function listThreads() {
  const objectStore = await store("readonly");
  const all = await wrap(objectStore.getAll());
  return all.sort((a, b) => b.updatedAt - a.updatedAt);
}

// The most recently updated thread, or null when the store is empty.
export async function newestThread() {
  const threads = await listThreads();
  return threads[0] || null;
}
