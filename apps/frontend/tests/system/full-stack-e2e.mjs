import { chromium } from "@playwright/test";
import { createServer } from "node:net";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SCRIPT_DIR, "../../../..");
const PRODUCTION_E2E = process.env.PRODUCTION_E2E === "1";
const COMPOSE_FILES = PRODUCTION_E2E
  ? ["-f", join(REPOSITORY_ROOT, "compose.production.yaml"), "-f", join(SCRIPT_DIR, "compose.production-e2e.yaml")]
  : ["-f", join(REPOSITORY_ROOT, "compose.yaml"), "-f", join(SCRIPT_DIR, "compose.e2e.yaml")];
const RUN_TIMEOUT_MS = 15 * 60_000;
const READY_TIMEOUT_MS = 10 * 60_000;
const JOB_TIMEOUT_MS = 4 * 60_000;
const SAFE_PROJECT = /^framee2e_[a-z0-9][a-z0-9_-]{5,48}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TERMINAL = new Set(["SUCCEEDED", "FAILED"]);

function projectName() {
  const supplied = process.env.FULL_STACK_E2E_PROJECT;
  const generated = `framee2e_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
  const value = (supplied || generated).toLowerCase();
  if (!SAFE_PROJECT.test(value)) throw new Error("FULL_STACK_E2E_PROJECT güvenli biçimde değil.");
  return value;
}

function safeText(value) {
  return String(value)
    .replace(/([?&](?:x-amz-[^=&\s]+|signature|token|credential)=)[^&\s]+/gi, "$1[REDACTED]")
    .replace(/\bfile:\/\/\/[a-z]:\/[^\s"'<>|]+/gi, "[REDACTED]")
    .replace(/\b[a-z]:\\[^\r\n"'<>|]+/gi, "[REDACTED]")
    .replace(/\bs3:\/\/[^\s"'<>]+/gi, "[REDACTED]")
    .replace(/\be2e-[a-z0-9-]+(?:\/[^\s"'<>]+)?/gi, "[REDACTED]")
    .replace(/\/(?:tmp|var\/tmp)\/frame-full-stack-e2e-[^\s"'<>]*/gi, "[REDACTED]")
    .replace(/(["']?(?:bucket(?:_name)?|object(?:_key|_name|_path)?|storage_prefix)["']?\s*[:=]\s*)["']?[^\s,"'}\]]+["']?/gi, "$1[REDACTED]")
    .replace(/(?:postgres|redis|minio|backend|outbox-publisher|frame-worker):\d+/gi, "[REDACTED]")
    .replace(/https?:\/\/(?:127\.0\.0\.1|10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})(?::\d+)?/gi, "[REDACTED]")
    .replace(/(?:postgresql(?:\+psycopg)?|redis):\/\/[^\s]+/gi, "[REDACTED]")
    .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "[REDACTED]")
    .split(/\r?\n/)
    .slice(-80)
    .join("\n");
}

function verifyRedaction() {
  const sensitive = [
    "e2e-fake-bucket/private/prefix/frame.jpg",
    "fake-bucket-name",
    "private/object/key.jpg",
    "another/private/key.jpg",
    "s3://fake-bucket-name/private/object/key.jpg",
    "C:\\Users\\Fixture\\AppData\\Local\\Temp\\frame-full-stack-e2e-fake\\fixture.mp4",
    "file:///C:/Users/Fixture/AppData/Local/Temp/frame-full-stack-e2e-fake/fixture.mp4",
    "/tmp/frame-full-stack-e2e-fake/fixture.mp4",
    "fake-signature",
  ];
  const sample = [
    "frame-worker-1 | ready",
    "CONTAINERS=0",
    `bucket=e2e-fake-bucket/private/prefix/frame.jpg bucket_name=fake-bucket-name`,
    `object_key=private/object/key.jpg storage_prefix=another/private/key.jpg`,
    `source=s3://fake-bucket-name/private/object/key.jpg`,
    `windows=C:\\Users\\Fixture\\AppData\\Local\\Temp\\frame-full-stack-e2e-fake\\fixture.mp4`,
    `file_uri=file:///C:/Users/Fixture/AppData/Local/Temp/frame-full-stack-e2e-fake/fixture.mp4`,
    `temporary=/tmp/frame-full-stack-e2e-fake/fixture.mp4`,
    `url=https://127.0.0.1:9000/frame.jpg?X-Amz-Signature=fake-signature`,
  ].join("\n");
  const redacted = safeText(sample);
  if (sensitive.some((item) => redacted.includes(item))) throw new Error("Tanı redaksiyonu sahte hassas değeri maskelemedi.");
  if (!redacted.includes("frame-worker-1 | ready") || !redacted.includes("CONTAINERS=0")) {
    throw new Error("Tanı redaksiyonu güvenli operasyon kanıtını korumadı.");
  }
  if (!redacted.includes("[REDACTED]")) throw new Error("Tanı redaksiyonu sabit maske üretmedi.");
  console.log("Tanı redaksiyonu sahte örneklerle doğrulandı.");
}

function run(command, args, { env, timeoutMs = 120_000, capture = true, allowFailure = false } = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, {
      cwd: REPOSITORY_ROOT,
      env: env ?? process.env,
      windowsHide: true,
      stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
    });
    let stdout = "";
    let stderr = "";
    if (capture) {
      child.stdout.on("data", (chunk) => { stdout += chunk; });
      child.stderr.on("data", (chunk) => { stderr += chunk; });
    }
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, timeoutMs);
    child.once("error", (error) => {
      clearTimeout(timer);
      rejectPromise(error);
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      const result = { code: code ?? -1, stdout: stdout.trim(), stderr: stderr.trim() };
      if (!allowFailure && (timedOut || code !== 0)) {
        rejectPromise(new Error(`${command} başarısız oldu${timedOut ? " (timeout)" : ` (exit ${code})`}: ${safeText(stderr || stdout)}`));
      } else resolvePromise(result);
    });
  });
}

function docker(project, env, args, options) {
  return run("docker", ["compose", "-p", project, ...COMPOSE_FILES, ...args], { env, ...options });
}

async function freePort() {
  return new Promise((resolvePort, rejectPort) => {
    const server = createServer();
    server.unref();
    server.once("error", rejectPort);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") return rejectPort(new Error("Geçici port ayrılamadı."));
      const port = address.port;
      server.close((error) => error ? rejectPort(error) : resolvePort(port));
    });
  });
}

async function baselineContainers() {
  const result = await run("docker", ["ps", "-a", "--format", "{{.ID}}|{{.State}}"], { timeoutMs: 30_000 });
  return new Map(result.stdout.split(/\r?\n/).filter(Boolean).map((line) => line.split("|")));
}

async function projectResources(project) {
  const commands = [
    ["ps", "-aq", "--filter", `label=com.docker.compose.project=${project}`],
    ["network", "ls", "-q", "--filter", `label=com.docker.compose.project=${project}`],
    ["volume", "ls", "-q", "--filter", `label=com.docker.compose.project=${project}`],
  ];
  const results = [];
  for (const args of commands) results.push((await run("docker", args, { timeoutMs: 30_000, allowFailure: true })).stdout);
  return results.flatMap((item) => item.split(/\r?\n/).filter(Boolean));
}

async function cleanup(project, env, temporaryDirectory, baseline) {
  await docker(project, env, ["down", "--volumes", "--remove-orphans", "--rmi", "local", "--timeout", "10"], { timeoutMs: 120_000, allowFailure: true });
  if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true });
  const leaked = await projectResources(project);
  if (leaked.length) throw new Error(`İzole Docker cleanup tamamlanmadı (${leaked.length} kaynak).`);
  if (baseline) {
    const current = await baselineContainers();
    for (const [id, state] of baseline) {
      if (!current.has(id) || current.get(id) !== state) throw new Error("Başlangıçtaki Docker container durumu değişti.");
    }
  }
}

async function diagnostics(project, env) {
  const ps = await docker(project, env, ["ps", "-a"], { timeoutMs: 30_000, allowFailure: true });
  const logs = await docker(project, env, ["logs", "--no-color", "--tail", "80"], { timeoutMs: 30_000, allowFailure: true });
  console.error("Güvenli Docker tanısı:\n" + safeText(`${ps.stdout}\n${ps.stderr}\n${logs.stdout}\n${logs.stderr}`));
}

function manifestIsPublic(value, jobId) {
  if (!value || typeof value !== "object" || value.job_id !== jobId || !Array.isArray(value.frames) || value.frames.length < 1) return false;
  const exactKeys = (candidate, expected) => {
    const actual = Object.keys(candidate).sort();
    return actual.length === expected.length && actual.every((key, index) => key === [...expected].sort()[index]);
  };
  if (!exactKeys(value, ["schema_version", "job_id", "created_at", "summary", "frames"])) return false;
  if (!value.summary || typeof value.summary !== "object" || !exactKeys(value.summary, ["frames_saved", "candidates", "shortlisted", "duplicates_removed", "processing_seconds", "duration_seconds"])) return false;
  const frameKeys = ["index", "filename", "content_type", "size_bytes", "sha256", "timestamp_ms", "width", "height", "access_url"];
  if (!value.frames.every((frame) => frame && typeof frame === "object" && exactKeys(frame, frameKeys))) return false;
  const forbidden = /^(?:bucket|object_key|result_reference|run_token|storage_endpoint|presigned_url)$/i;
  const visit = (candidate) => {
    if (Array.isArray(candidate)) return candidate.every(visit);
    if (!candidate || typeof candidate !== "object") return true;
    return Object.entries(candidate).every(([key, child]) => !forbidden.test(key) && visit(child));
  };
  return visit(value);
}


async function inspectZip(path) {
  const script = [
    "import hashlib,json,re,sys,zipfile",
    "z=zipfile.ZipFile(sys.argv[1])",
    "names=z.namelist()",
    "assert names and names[-1]=='manifest.json'",
    "assert all(re.fullmatch(r'[A-Za-z0-9_.-]+',n) and '..' not in n for n in names)",
    "manifest=json.loads(z.read('manifest.json'))",
    "hashes={n:hashlib.sha256(z.read(n)).hexdigest() for n in names if n!='manifest.json'}",
    "print(json.dumps({'names':names,'manifest':manifest,'hashes':hashes},separators=(',',':')))",
  ].join(";");
  const result = await run("python", ["-c", script, path], { timeoutMs: 30_000 });
  return JSON.parse(result.stdout);
}

async function inspectDatasetZip(path, mode) {
  const script = [
    "import hashlib,json,re,sys,zipfile",
    "z=zipfile.ZipFile(sys.argv[1])",
    "names=z.namelist()",
    "assert names and 'manifest.json' in names",
    "assert all(re.fullmatch(r'(?:images/)?[A-Za-z0-9_.-]+',n) and '..' not in n for n in names)",
    "m=json.loads(z.read('manifest.json'))",
    "assert 'run_token' not in m and 'object_key' not in json.dumps(m)",
    "files=[n for n in names if n!='manifest.json']",
    "assert len(files)==len(m['images'])",
    "items={i['filename']:i for i in m['images']}",
    "assert set(items)=={n.rsplit('/',1)[-1] for n in files}",
    "assert all(len(z.read(n))==items[n.rsplit('/',1)[-1]]['size_bytes'] for n in files)",
    "assert all(hashlib.sha256(z.read(n)).hexdigest()==items[n.rsplit('/',1)[-1]]['sha256'] for n in files)",
    "def dims(d):\n i=2\n while i+9<len(d):\n  if d[i]!=255: i+=1; continue\n  marker=d[i+1]; size=int.from_bytes(d[i+2:i+4],'big')\n  if marker in (192,194): return (int.from_bytes(d[i+7:i+9],'big'),int.from_bytes(d[i+5:i+7],'big'))\n  i+=2+size\n raise AssertionError('JPEG dimensions missing')",
    "assert sys.argv[2]!='yolo' or all(dims(z.read(n))==(640,640) for n in files)",
    "print(json.dumps({'count':len(files),'manifest':m},separators=(',',':')))"
  ].join("\n");
  const result = await run("python", ["-c", script, path, mode], { timeoutMs: 30_000 });
  return JSON.parse(result.stdout);
}

async function datasetBrowserFlow(baseUrl, paths, archive, temporaryDirectory) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  const statuses = [];
  try {
    await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60_000 });
    await page.getByRole("tab", { name: "Görsel veri seti" }).click();
    if (archive) await page.getByRole("radio", { name: "ZIP arşivi" }).click();
    await page.locator(".drop-zone input[type=file]").setInputFiles(paths);
    const upload = page.waitForResponse((response) => response.request().method() === "POST" && /\/api\/v1\/jobs\/image-dataset\/?$/.test(new URL(response.url()).pathname), { timeout: 60_000 });
    await page.locator('form button[type="submit"]').click();
    const uploadResponse = await upload;
    if (uploadResponse.status() !== 202) throw new Error(`Dataset upload HTTP ${uploadResponse.status()}.`);
    const submitted = await uploadResponse.json();
    const jobId = String(submitted.job_id || "").toLowerCase();
    if (!UUID.test(jobId)) throw new Error("Dataset job ID geçersiz.");
    const deadline = Date.now() + JOB_TIMEOUT_MS;
    let terminal;
    while (Date.now() < deadline) {
      const response = await context.request.get(`${baseUrl}/api/v1/jobs/${jobId}`);
      const body = await response.json();
      if (body.status && !statuses.includes(body.status)) statuses.push(body.status);
      if (TERMINAL.has(body.status)) { terminal = body.status; break; }
      await page.waitForTimeout(250);
    }
    if (terminal !== "SUCCEEDED") throw new Error(`Dataset terminal=${terminal ?? "timeout"}.`);
    const resultResponse = await context.request.get(`${baseUrl}/api/v1/jobs/${jobId}/result`);
    const manifest = await resultResponse.json();
    if (!resultResponse.ok() || manifest.dataset_type !== "image" || !Array.isArray(manifest.images) || manifest.images.length < 1) throw new Error("Dataset public manifest geçersiz.");
    if (/bucket|object_key|run_token|credential|presigned/i.test(JSON.stringify(manifest))) throw new Error("Dataset public manifest internal alan içeriyor.");
    await page.goto(`${baseUrl}/jobs/${jobId}/result`, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await page.getByRole("heading", { name: "Veri seti analizi" }).waitFor({ state: "visible", timeout: 30_000 });
    const preview = page.locator(".dataset-card img").first();
    await preview.waitFor({ state: "visible", timeout: 30_000 });
    await preview.evaluate((node) => new Promise((resolveImage, rejectImage) => { if (node.complete && node.naturalWidth > 0) return resolveImage(); node.addEventListener("load", resolveImage, { once: true }); node.addEventListener("error", rejectImage, { once: true }); }));
    for (const [mode, url] of [["accepted", manifest.accepted_download_url], ["yolo", manifest.yolo_download_url]]) {
      const response = await context.request.get(`${baseUrl}${url}`);
      if (!response.ok() || response.headers()["content-type"] !== "application/zip") throw new Error(`${mode} ZIP indirilemedi.`);
      const path = join(temporaryDirectory, `${jobId}-${mode}.zip`);
      await writeFile(path, await response.body());
      const inspected = await inspectDatasetZip(path, mode);
      if (mode === "yolo" && inspected.count !== manifest.summary.recommended_count) throw new Error("YOLO öneri sayısı uyuşmuyor.");
    }
    return { jobId, statuses: statuses.join(" -> "), images: manifest.images.length };
  } finally {
    await context.close();
    await browser.close();
  }
}

function assertExportManifestIsPublic(manifest, jobId) {
  if (!manifest || manifest.schema_version !== 1 || manifest.job_id !== jobId || !UUID.test(manifest.export_id) || !Array.isArray(manifest.frames)) {
    throw new Error("ZIP public manifest sözleşmesi geçersiz.");
  }
  if (/bucket|object_key|storage|endpoint|credential|presigned|local_path/i.test(JSON.stringify(manifest))) {
    throw new Error("ZIP manifest dahili storage bilgisi içeriyor.");
  }
}

async function browserFlow(baseUrl, videoPath) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  const statuses = [];
  let frameAccessCount = 0;
  page.on("response", async (response) => {
    const request = response.request();
    const pathname = new URL(response.url()).pathname;
    if (request.method() === "POST" && /\/result\/frames\/\d+\/access$/.test(pathname) && response.ok()) frameAccessCount += 1;
    const isUpload = request.method() === "POST" && /\/api\/v1\/jobs\/upload\/?$/.test(pathname);
    const isStatus = request.method() === "GET" && /\/api\/v1\/jobs\/[0-9a-f-]+\/?$/.test(pathname);
    if ((!isUpload && !isStatus) || !response.ok()) return;
    try {
      const body = await response.json();
      if (typeof body?.status === "string" && !statuses.includes(body.status)) statuses.push(body.status);
    } catch { /* A malformed response is handled by the production UI. */ }
  });
  try {
    await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60_000 });
    await page.locator("#video-file").setInputFiles(videoPath);
    await page.locator("#candidate-fps").fill("4");
    await page.locator("#window-seconds").fill("1");
    const uploadResponsePromise = page.waitForResponse((response) => {
      const pathname = new URL(response.url()).pathname;
      return response.request().method() === "POST" && /\/api\/v1\/jobs\/upload\/?$/.test(pathname);
    }, { timeout: 60_000 });
    await page.locator('form button[type="submit"]').click();
    const uploadResponse = await uploadResponsePromise;
    if (uploadResponse.status() !== 202) throw new Error(`Upload isteği başarısız oldu (HTTP ${uploadResponse.status()}; 202 bekleniyordu).`);
    const uploadBody = await uploadResponse.json();
    const submittedJobId = typeof uploadBody?.job_id === "string" ? uploadBody.job_id.toLowerCase() : "";
    if (!UUID.test(submittedJobId) || uploadBody?.status !== "PENDING_DISPATCH") {
      throw new Error("Upload geçerli bir PENDING_DISPATCH işi döndürmedi.");
    }
    const pendingExportStatus = await page.evaluate(async (id) => {
      const response = await fetch(`/api/v1/jobs/${encodeURIComponent(id)}/exports`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "all" }),
      });
      return response.status;
    }, submittedJobId);
    if (pendingExportStatus !== 409) throw new Error("SUCCEEDED olmayan job export reddedilmedi.");
    if (!statuses.includes(uploadBody.status)) statuses.push(uploadBody.status);
    await Promise.race([
      page.waitForURL(/\/jobs\/[0-9a-f-]+$/, { timeout: 60_000 }),
      page.locator(".alert.error").waitFor({ state: "visible", timeout: 60_000 }).then(() => { throw new Error("Upload formu güvenli kullanıcı hatası gösterdi."); }),
    ]);
    const jobId = new URL(page.url()).pathname.split("/").at(-1)?.toLowerCase() ?? "";
    if (!UUID.test(jobId) || jobId !== submittedJobId) throw new Error("Upload ve yönlendirme job ID değerleri eşleşmedi.");

    const deadline = Date.now() + JOB_TIMEOUT_MS;
    let terminal = null;
    while (Date.now() < deadline) {
      const observed = await page.evaluate(async (id) => {
        const response = await fetch(`/api/v1/jobs/${encodeURIComponent(id)}`, { cache: "no-store" });
        if (!response.ok) return { status: null, http: response.status };
        const body = await response.json();
        return { status: typeof body?.status === "string" ? body.status : null, http: response.status };
      }, jobId);
      if (observed.status && !statuses.includes(observed.status)) statuses.push(observed.status);
      if (observed.status && TERMINAL.has(observed.status)) { terminal = observed.status; break; }
      await page.waitForTimeout(250);
    }
    if (terminal !== "SUCCEEDED") throw new Error(`Job başarıyla tamamlanmadı (terminal=${terminal ?? "timeout"}).`);
    if (statuses[0] !== "PENDING_DISPATCH" || !statuses.some((status) => status === "QUEUED" || status === "RUNNING")) {
      throw new Error(`Gerçek durum geçişleri eksik: ${statuses.join(" -> ")}`);
    }

    const resultLink = page.locator("a.result-link");
    await resultLink.waitFor({ state: "visible", timeout: 10_000 });
    await resultLink.click();
    await page.waitForURL(new RegExp(`/jobs/${jobId}/result$`), { timeout: 30_000 });
    await page.locator("#summary-title").waitFor({ state: "visible", timeout: 30_000 });
    const framesSaved = Number(await page.locator(".summary-grid dd").first().textContent());
    if (!Number.isSafeInteger(framesSaved) || framesSaved < 1) throw new Error("Analiz özeti en az bir kaydedilmiş kare göstermiyor.");
    const image = page.locator(".frame-card img").first();
    await image.waitFor({ state: "visible", timeout: 30_000 });
    await image.evaluate((node) => new Promise((resolveImage, rejectImage) => {
      if (node.complete && node.naturalWidth > 0) return resolveImage();
      node.addEventListener("load", () => resolveImage(), { once: true });
      node.addEventListener("error", () => rejectImage(new Error("Frame görseli yüklenemedi.")), { once: true });
    }));
    if (frameAccessCount < 1) throw new Error("Frame access endpoint akışta doğrulanmadı.");

    const downloadEvent = page.waitForEvent("download", { timeout: 30_000 });
    await page.getByRole("button", { name: /manifest/i }).click();
    const download = await downloadEvent;
    const downloadPath = await download.path();
    if (!downloadPath) throw new Error("Manifest download dosyası oluşmadı.");
    const manifest = JSON.parse(await readFile(downloadPath, "utf8"));
    if (!manifestIsPublic(manifest, jobId)) throw new Error("İndirilen public manifest doğrulanamadı.");
    if (!download.suggestedFilename().includes(jobId)) throw new Error("Manifest dosya adı job ilişkisini korumuyor.");

    const frameDownloadPromise = page.waitForEvent("download", { timeout: 30_000 });
    await page.getByRole("link", { name: "İndir" }).first().click();
    const frameDownload = await frameDownloadPromise;
    const frameResponse = await context.request.get(`${baseUrl}/api/v1/jobs/${encodeURIComponent(jobId)}/result/frames/0/download`, { timeout: 30_000 });
    const framePath = await frameDownload.path();
    if (!framePath || frameResponse.headers()["content-type"] !== "image/jpeg") throw new Error("Tek frame download response geçersiz.");
    const disposition = frameResponse.headers()["content-disposition"] ?? "";
    if (!/^attachment; filename="frame_0001_\d{2}-\d{2}-\d{2}\.\d{3}\.jpg"$/.test(disposition)) throw new Error("Tek frame Content-Disposition geçersiz.");
    const frameBytes = await frameResponse.body();
    if (createHash("sha256").update(frameBytes).digest("hex") !== manifest.frames[0].sha256) throw new Error("Tek frame SHA-256 uyuşmuyor.");

    const selectedIndices = [0, 1];
    const invalidStatuses = await page.evaluate(async ({ id, missing }) => {
      const payloads = [
        { mode: "selected", frame_indices: [] },
        { mode: "selected", frame_indices: [0, 0] },
        { mode: "selected", frame_indices: [-1] },
        { mode: "selected", frame_indices: [missing] },
      ];
      return Promise.all(payloads.map(async (body) => (await fetch(`/api/v1/jobs/${encodeURIComponent(id)}/exports`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      })).status));
    }, { id: jobId, missing: manifest.frames.length });
    if (invalidStatuses.some((status) => status !== 422)) throw new Error("Geçersiz frame seçimleri strict reddedilmedi.");
    const prepared = await page.evaluate(async ({ id, indices }) => {
      const created = await fetch(`/api/v1/jobs/${encodeURIComponent(id)}/exports`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "selected", frame_indices: indices }) });
      const body = await created.json();
      const download = await fetch(body.download_url ?? `/api/v1/jobs/${encodeURIComponent(id)}/exports/${encodeURIComponent(body.id)}/download`, { redirect: "manual" });
      return { createStatus: created.status, downloadStatus: download.status, body };
    }, { id: jobId, indices: selectedIndices });
    if (prepared.createStatus !== 202 || prepared.downloadStatus !== 409 || !UUID.test(prepared.body?.id ?? "")) throw new Error("Hazır olmayan export download sözleşmesi geçersiz.");
    for (const index of selectedIndices) await page.getByRole("checkbox", { name: `Kare ${index + 1} seç` }).check();
    const selectedCreatePromise = page.waitForResponse((response) => response.request().method() === "POST" && /\/exports$/.test(new URL(response.url()).pathname), { timeout: 30_000 });
    const selectedDownloadPromise = page.waitForEvent("download", { timeout: 120_000 });
    await page.getByRole("button", { name: "Seçilenleri ZIP indir" }).click();
    const selectedCreated = await selectedCreatePromise;
    if (selectedCreated.status() !== 202) throw new Error("Seçili export 202 dönmedi.");
    const selectedBody = await selectedCreated.json();
    if (selectedBody?.id !== prepared.body.id || !["PREPARING", "READY"].includes(selectedBody?.status)) throw new Error("Seçili export idempotent DTO geçersiz.");
    const selectedDownload = await selectedDownloadPromise;
    const selectedPath = await selectedDownload.path();
    if (!selectedPath) throw new Error("Seçili ZIP indirilemedi.");
    const selectedZip = await inspectZip(selectedPath);
    assertExportManifestIsPublic(selectedZip.manifest, jobId);
    if (selectedZip.names.length !== selectedIndices.length + 1 || selectedZip.manifest.frames.length !== selectedIndices.length) throw new Error("Seçili ZIP entry sayısı geçersiz.");
    for (const [position, sourceIndex] of selectedIndices.entries()) {
      const exported = selectedZip.manifest.frames[position];
      if (exported.index !== sourceIndex || selectedZip.hashes[exported.filename] !== manifest.frames[sourceIndex].sha256) throw new Error("Seçili ZIP frame/hash sırası geçersiz.");
    }
    if (selectedZip.manifest.frames.some((frame) => frame.index === 2)) throw new Error("Seçilmeyen frame ZIP içinde.");
    const repeated = await page.evaluate(async ({ id, indices }) => {
      const response = await fetch(`/api/v1/jobs/${encodeURIComponent(id)}/exports`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "selected", frame_indices: indices }) });
      return { status: response.status, body: await response.json() };
    }, { id: jobId, indices: selectedIndices });
    if (repeated.status !== 202 || repeated.body?.id !== selectedBody.id) throw new Error("Export idempotency doğrulanamadı.");

    const allCreatePromise = page.waitForResponse((response) => response.request().method() === "POST" && /\/exports$/.test(new URL(response.url()).pathname), { timeout: 30_000 });
    const allDownloadPromise = page.waitForEvent("download", { timeout: 120_000 });
    await page.getByRole("button", { name: "Tüm frame’leri ZIP indir" }).click();
    if ((await allCreatePromise).status() !== 202) throw new Error("Tüm-frame export 202 dönmedi.");
    const allDownload = await allDownloadPromise;
    const allPath = await allDownload.path();
    if (!allPath) throw new Error("Tüm-frame ZIP indirilemedi.");
    const allZip = await inspectZip(allPath);
    assertExportManifestIsPublic(allZip.manifest, jobId);
    if (allZip.manifest.frames.length !== manifest.frames.length) throw new Error("Tüm-frame ZIP sayısı geçersiz.");
    for (const [index, exported] of allZip.manifest.frames.entries()) {
      if (exported.index !== index || allZip.hashes[exported.filename] !== manifest.frames[index].sha256) throw new Error("Tüm-frame ZIP sıra/hash geçersiz.");
    }
    return { statuses: statuses.join(" -> "), framesSaved, jobId };
  } finally {
    await context.close();
    await browser.close();
  }
}

async function waitForService(project, env, service, expected = "healthy", timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const container = await docker(project, env, ["ps", "-q", service], { timeoutMs: 30_000, allowFailure: true });
    if (container.stdout) {
      const state = await run("docker", ["inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container.stdout], { timeoutMs: 30_000, allowFailure: true });
      if (state.stdout === expected) return;
      if (["exited", "dead"].includes(state.stdout)) throw new Error(`${service} kontrollü restart sırasında durdu.`);
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 500));
  }
  throw new Error(`${service} kontrollü restart sonrasında hazır olmadı.`);
}

async function verifyPersistedResultPage(baseUrl, jobId) {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  try {
    await page.goto(`${baseUrl}/jobs/${jobId}/result`, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await page.locator("#summary-title").waitFor({ state: "visible", timeout: 30_000 });
    const image = page.locator(".frame-card img").first();
    await image.waitFor({ state: "visible", timeout: 30_000 });
    await image.evaluate((node) => new Promise((resolveImage, rejectImage) => {
      if (node.complete && node.naturalWidth > 0) return resolveImage();
      node.addEventListener("load", () => resolveImage(), { once: true });
      node.addEventListener("error", () => rejectImage(new Error("Kalıcı frame görseli yüklenemedi.")), { once: true });
    }));
  } finally {
    await page.close();
    await browser.close();
  }
}

async function main() {
  const project = projectName();
  const mode = process.argv[2] ?? "run";
  if (mode === "--redaction-self-test") return verifyRedaction();
  const publicHost = process.env.FULL_STACK_E2E_PUBLIC_HOST ?? "127.0.0.1";
  if (!["127.0.0.1", "host.docker.internal"].includes(publicHost)) throw new Error("FULL_STACK_E2E_PUBLIC_HOST izin verilen bir host değil.");
  const frontendPort = await freePort();
  let minioPort = await freePort();
  while (minioPort === frontendPort) minioPort = await freePort();
  const fakePassword = `fixture_${project}_only`;
  const env = {
    ...process.env,
    FULL_STACK_E2E_FRONTEND_PORT: String(frontendPort),
    FULL_STACK_E2E_MINIO_PORT: String(minioPort),
    POSTGRES_DB: `e2e_${project.slice(-18)}`,
    POSTGRES_USER: "e2e_fixture_user",
    POSTGRES_PASSWORD: fakePassword,
    JOB_SOURCE_ENCRYPTION_KEY: Buffer.alloc(32, 37).toString("base64"),
    OBJECT_STORAGE_ACCESS_KEY: "e2e_fixture_access",
    OBJECT_STORAGE_SECRET_KEY: fakePassword,
    OBJECT_STORAGE_BUCKET: `e2e-${project.slice(-24).replaceAll("_", "-")}`,
    MINIO_ROOT_USER: "e2e_fixture_access",
    MINIO_ROOT_PASSWORD: fakePassword,
    INTERNAL_PROXY_SHARED_SECRET: `proxy_${project}_0123456789abcdef`,
    MINIO_BUCKET: `e2e-${project.slice(-24).replaceAll("_", "-")}`,
    OBJECT_STORAGE_EXTERNAL_ENDPOINT: `http://${publicHost}:${minioPort}`,
    APP_SITE: "http://127.0.0.1",
    STORAGE_SITE: "http://storage.localhost",
    PROXY_HTTP_BIND: `127.0.0.1:${frontendPort}`,
    PROXY_HTTPS_BIND: `127.0.0.1:${minioPort}`,
  };
  if (mode === "--cleanup-only") return cleanup(project, env, null, null);
  if (mode === "--diagnostics-only") return diagnostics(project, env);
  if (mode !== "run") throw new Error(`Bilinmeyen mod: ${mode}`);

  const baseline = await baselineContainers();
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "frame-full-stack-e2e-"));
  const videoPath = join(temporaryDirectory, "fixture.mp4");
  const imagePaths = ["fixture.jpg", "fixture.png", "fixture.webp"].map((name) => join(temporaryDirectory, name));
  const archivePath = join(temporaryDirectory, "fixture-images.zip");
  let cleaning = false;
  const emergencyCleanup = async (exitCode) => {
    if (cleaning) return;
    cleaning = true;
    try { await cleanup(project, env, temporaryDirectory, baseline); } finally { process.exit(exitCode); }
  };
  process.once("SIGINT", () => { void emergencyCleanup(130); });
  process.once("SIGTERM", () => { void emergencyCleanup(143); });

  const globalTimer = setTimeout(() => { void emergencyCleanup(124); }, RUN_TIMEOUT_MS);
  const started = Date.now();
  try {
    await run("docker", ["info", "--format", "{{.ServerVersion}}"], { timeoutMs: 30_000 });
    await docker(project, env, ["config", "--quiet"], { timeoutMs: 30_000 });
    await docker(project, env, ["up", "-d", "--build", "--wait", "--wait-timeout", String(Math.ceil(READY_TIMEOUT_MS / 1000))], { timeoutMs: READY_TIMEOUT_MS });
    const expectedServices = PRODUCTION_E2E
      ? ["proxy", "frontend", "migrate", "backend", "outbox-publisher", "frame-worker", "postgres", "redis", "minio", "minio-init"]
      : ["frontend", "backend", "outbox-publisher", "frame-worker", "postgres", "redis", "minio", "minio-init"];
    for (const service of expectedServices) {
      const container = await docker(project, env, ["ps", "-a", "-q", service], { timeoutMs: 30_000 });
      if (!/^[0-9a-f]{12,64}$/.test(container.stdout)) throw new Error(`Beklenen production servisi bulunamadı: ${service}`);
    }
    if (PRODUCTION_E2E) {
      const migrateContainer = (await docker(project, env, ["ps", "-a", "-q", "migrate"], { timeoutMs: 30_000 })).stdout;
      const migrationExit = await run("docker", ["inspect", "--format", "{{.State.ExitCode}}", migrateContainer], { timeoutMs: 30_000 });
      if (migrationExit.stdout !== "0") throw new Error(`Migration exit code 0 değil: ${migrationExit.stdout}`);
    }
    const readiness = await fetch(`http://${publicHost}:${frontendPort}/api/v1/ready`);
    if (!readiness.ok || JSON.stringify(await readiness.json()) !== '{"status":"ready"}') throw new Error("Reverse proxy readiness tam ready cevabı üretmedi.");
    const worker = await docker(project, env, ["ps", "-q", "frame-worker"], { timeoutMs: 30_000 });
    if (!/^[0-9a-f]{12,64}$/.test(worker.stdout)) throw new Error("Doğrulanmış worker container bulunamadı.");
    await docker(project, env, ["exec", "-T", "frame-worker", "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=12:duration=4", "-vf", "eq=brightness=0.10:saturation=1.15", "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", "/tmp/e2e-fixture.mp4"], { timeoutMs: 60_000 });
    await run("docker", ["cp", `${worker.stdout}:/tmp/e2e-fixture.mp4`, videoPath], { timeoutMs: 30_000 });
    for (const [index, extension] of ["jpg", "png", "webp"].entries()) {
      await docker(project, env, ["exec", "-T", "frame-worker", "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", `testsrc2=size=${320 + index * 40}x${180 + index * 20}:rate=1:duration=1`, "-frames:v", "1", "-threads", "1", "-y", `/tmp/e2e-image.${extension}`], { timeoutMs: 60_000 });
      await run("docker", ["cp", `${worker.stdout}:/tmp/e2e-image.${extension}`, imagePaths[index]], { timeoutMs: 30_000 });
    }
    await run("python", ["-c", "import os,sys,zipfile;z=zipfile.ZipFile(sys.argv[1],'w',zipfile.ZIP_DEFLATED);[z.write(p,'safe/'+os.path.basename(p)) for p in sys.argv[2:]];z.close()", archivePath, ...imagePaths], { timeoutMs: 30_000 });
    const result = await browserFlow(`http://${publicHost}:${frontendPort}`, videoPath);
    if (result.framesSaved !== 4) throw new Error(`Deterministik fixture tam 4 frame üretmedi: ${result.framesSaved}`);
    const datasetSingle = await datasetBrowserFlow(`http://${publicHost}:${frontendPort}`, [imagePaths[0]], false, temporaryDirectory);
    const datasetMultiple = await datasetBrowserFlow(`http://${publicHost}:${frontendPort}`, imagePaths, false, temporaryDirectory);
    const datasetZip = await datasetBrowserFlow(`http://${publicHost}:${frontendPort}`, [archivePath], true, temporaryDirectory);
    if (PRODUCTION_E2E) {
      const postgresBefore = (await docker(project, env, ["ps", "-q", "postgres"], { timeoutMs: 30_000 })).stdout;
      const minioBefore = (await docker(project, env, ["ps", "-q", "minio"], { timeoutMs: 30_000 })).stdout;
      const restartServices = ["redis", "backend", "outbox-publisher", "frame-worker", "frontend", "proxy"];
      await docker(project, env, ["stop", "--timeout", "10", ...restartServices], { timeoutMs: 120_000 });
      await docker(project, env, ["up", "-d", "--force-recreate", "--no-deps", "postgres", "minio"], { timeoutMs: 120_000 });
      await waitForService(project, env, "postgres");
      await waitForService(project, env, "minio");
      await docker(project, env, ["run", "--rm", "minio-init"], { timeoutMs: 60_000 });
      await docker(project, env, ["start", "redis"], { timeoutMs: 30_000 });
      await waitForService(project, env, "redis");
      await docker(project, env, ["start", "backend"], { timeoutMs: 30_000 });
      await waitForService(project, env, "backend");
      await docker(project, env, ["start", "outbox-publisher", "frame-worker", "frontend"], { timeoutMs: 60_000 });
      await waitForService(project, env, "outbox-publisher", "running");
      await waitForService(project, env, "frame-worker", "running");
      await waitForService(project, env, "frontend");
      await docker(project, env, ["start", "proxy"], { timeoutMs: 30_000 });
      await waitForService(project, env, "proxy");
      const postgresAfter = (await docker(project, env, ["ps", "-q", "postgres"], { timeoutMs: 30_000 })).stdout;
      const minioAfter = (await docker(project, env, ["ps", "-q", "minio"], { timeoutMs: 30_000 })).stdout;
      if (!postgresBefore || postgresAfter === postgresBefore || !minioBefore || minioAfter === minioBefore) {
        throw new Error("PostgreSQL veya MinIO kalıcılık testinde yeniden oluşturulmadı.");
      }
      const persisted = await fetch(`http://${publicHost}:${frontendPort}/api/v1/jobs/${result.jobId}`);
      const persistedBody = await persisted.json();
      if (!persisted.ok || persistedBody?.status !== "SUCCEEDED") {
        throw new Error("PostgreSQL kalıcılığı kontrollü restart sonrasında doğrulanamadı.");
      }
      const resultResponse = await fetch(`http://${publicHost}:${frontendPort}/api/v1/jobs/${result.jobId}/result`);
      const resultBody = await resultResponse.json();
      if (!resultResponse.ok || !Array.isArray(resultBody?.frames) || resultBody.frames.length < 1) {
        throw new Error("MinIO kalıcılığı kontrollü restart sonrasında doğrulanamadı.");
      }
      await verifyPersistedResultPage(`http://${publicHost}:${frontendPort}`, result.jobId);
      const manifestResponse = await fetch(`http://${publicHost}:${frontendPort}/api/v1/jobs/${result.jobId}/result/manifest`);
      const manifest = await manifestResponse.json();
      if (!manifestResponse.ok || !manifestIsPublic(manifest, result.jobId)) {
        throw new Error("Restart sonrası kalıcı manifest doğrulanamadı.");
      }
    }
    console.log(`Full-stack E2E başarılı: durumlar=${result.statuses}; kare=${result.framesSaved}; dataset=single:${datasetSingle.images},multi:${datasetMultiple.images},zip:${datasetZip.images}; container_recreation=${PRODUCTION_E2E ? "postgres,minio" : "none"}; süre=${((Date.now() - started) / 1000).toFixed(1)} sn.`);
  } catch (error) {
    await diagnostics(project, env);
    throw error;
  } finally {
    clearTimeout(globalTimer);
    cleaning = true;
    await cleanup(project, env, temporaryDirectory, baseline);
  }
}

main().catch((error) => {
  console.error(`Full-stack E2E başarısız: ${safeText(error instanceof Error ? error.message : error)}`);
  process.exitCode = 1;
});
