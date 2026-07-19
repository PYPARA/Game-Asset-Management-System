const DATABASE_NAME = "game-assets-credentials";
const STORE_NAME = "provider-credentials";
const DATABASE_VERSION = 1;

interface StoredCredential {
  id: string;
  key: CryptoKey;
  iv: ArrayBuffer;
  ciphertext: ArrayBuffer;
  updatedAt: string;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);

    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("无法打开凭据数据库"));
  });
}

async function runTransaction<T>(
  mode: IDBTransactionMode,
  action: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const database = await openDatabase();

  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, mode);
    const request = action(transaction.objectStore(STORE_NAME));

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("凭据操作失败"));
    transaction.oncomplete = () => database.close();
    transaction.onerror = () => reject(transaction.error ?? new Error("凭据事务失败"));
  });
}

export async function hasCredential(profileId: string): Promise<boolean> {
  const value = await runTransaction<StoredCredential | undefined>("readonly", (store) => store.get(profileId));
  return Boolean(value);
}

export async function saveCredential(profileId: string, apiKey: string): Promise<void> {
  if (!apiKey.trim()) throw new Error("API Key 不能为空");

  const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, [
    "encrypt",
    "decrypt",
  ]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(apiKey.trim()),
  );

  const record: StoredCredential = {
    id: profileId,
    key,
    iv: iv.buffer,
    ciphertext,
    updatedAt: new Date().toISOString(),
  };

  await runTransaction<IDBValidKey>("readwrite", (store) => store.put(record));
}

export async function readCredential(profileId: string): Promise<string | null> {
  const record = await runTransaction<StoredCredential | undefined>("readonly", (store) => store.get(profileId));
  if (!record) return null;

  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: new Uint8Array(record.iv) },
    record.key,
    record.ciphertext,
  );

  return new TextDecoder().decode(plaintext);
}

export async function deleteCredential(profileId: string): Promise<void> {
  await runTransaction<undefined>("readwrite", (store) => store.delete(profileId));
}

interface IpAddressClassification {
  isLoopback: boolean;
  isGlobal: boolean;
}

function classifyIpv4(hostname: string): IpAddressClassification | null {
  const parts = hostname.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d+$/.test(part))) return null;

  const octets = parts.map(Number);
  if (octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;

  const [first, second, third] = octets;
  const isLoopback = first === 127;
  const isNonGlobal =
    first === 0 ||
    first === 10 ||
    isLoopback ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 0 && third === 0) ||
    (first === 192 && second === 0 && third === 2) ||
    (first === 192 && second === 168) ||
    (first === 198 && (second === 18 || second === 19)) ||
    (first === 198 && second === 51 && third === 100) ||
    (first === 203 && second === 0 && third === 113) ||
    first >= 224;

  return { isLoopback, isGlobal: !isNonGlobal };
}

function classifyIpv6(hostname: string): IpAddressClassification | null {
  const address = hostname.replace(/^\[|\]$/g, "").toLocaleLowerCase();
  if (!address.includes(":")) return null;

  const isLoopback = address === "::1";
  const firstGroup = address.split(":", 1)[0];
  const isUniqueLocal = firstGroup.startsWith("fc") || firstGroup.startsWith("fd");
  const isLinkLocal = /^(?:fe[89ab])/.test(firstGroup);
  const isDocumentation = address.startsWith("2001:db8:") || address === "2001:db8::";
  const isUnspecified = address === "::";
  const isIpv4Mapped = address.startsWith("::ffff:");

  // Provider URLs should normally use public 2000::/3 addresses. The explicit
  // exclusions mirror Python's ipaddress.is_global for the ranges relevant to
  // provider endpoints; DNS rebinding and hostname resolution remain guarded
  // by the local backend immediately before every request.
  const isGlobalUnicast = /^[23]/.test(firstGroup);
  const isGlobal =
    isGlobalUnicast &&
    !isLoopback &&
    !isUniqueLocal &&
    !isLinkLocal &&
    !isDocumentation &&
    !isUnspecified &&
    !isIpv4Mapped;

  return { isLoopback, isGlobal };
}

function classifyIpAddress(hostname: string): IpAddressClassification | null {
  return classifyIpv4(hostname) ?? classifyIpv6(hostname);
}

/**
 * Validate and canonicalize a provider Base URL using the same static rules as
 * the FastAPI backend. Hostname DNS resolution is deliberately enforced again
 * by the backend because browsers cannot safely reproduce that check here.
 */
export function normalizeProviderUrl(rawUrl: string, allowPrivateNetwork: boolean): string | null {
  try {
    const candidate = rawUrl.trim();
    const rawAuthority = /^[a-z][a-z\d+.-]*:\/\/([^/?#]*)/i.exec(candidate)?.[1];
    const url = new URL(candidate);
    const hasUserInfo = Boolean(url.username || url.password || rawAuthority?.includes("@"));
    const hasQueryOrFragment = candidate.includes("?") || candidate.includes("#");
    if (!rawAuthority || !url.hostname || hasUserInfo || hasQueryOrFragment) return null;

    const hostname = url.hostname.replace(/^\[|\]$/g, "").toLocaleLowerCase();
    const isHttpLoopback = ["localhost", "127.0.0.1", "::1"].includes(hostname);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && isHttpLoopback)) return null;

    const address = classifyIpAddress(hostname);
    if (address && !address.isGlobal && !address.isLoopback && !allowPrivateNetwork) return null;

    const pathname = url.pathname.replace(/\/+$/, "");
    return `${url.protocol}//${url.host}${pathname}`;
  } catch {
    return null;
  }
}

export function isProviderUrlAllowed(rawUrl: string, allowPrivateNetwork: boolean): boolean {
  return normalizeProviderUrl(rawUrl, allowPrivateNetwork) !== null;
}
