import { useEffect, useMemo, useRef, useState } from "react";
import Hls from "hls.js";
import { motion } from "motion/react";
import {
  api,
  newIdempotencyKey,
  type LiveCctvSource,
  type NearLiveRun,
  type PublicCandidate,
  type PublicSource,
} from "../api/client";
import { useAuth } from "../console/AuthContext";

type InputMode = "live" | "upload" | "simulated";
type PreviewState = "idle" | "connecting" | "playing" | "reconnecting" | "paused" | "error";

const MAX_UPLOAD_BYTES = 64 * 1024 * 1024;
const DEMO_LOCATION_ID = "30000000-0000-4000-8000-000000000001";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const MODE_OPTIONS: Array<{
  id: InputMode;
  index: string;
  label: string;
  detail: string;
}> = [
  { id: "live", index: "01", label: "Connect live source", detail: "Approved HLS or edge connector" },
  { id: "upload", index: "02", label: "Upload video", detail: "Tenant-authorized MP4" },
  { id: "simulated", index: "03", label: "Simulated live", detail: "Generated road scene" },
];

function operationRank(stage?: string) {
  if (!stage) return 0;
  if (stage.includes("upload")) return 1;
  if (stage.includes("index")) return 2;
  if (stage.includes("analy")) return 3;
  if (stage.includes("review")) return 4;
  if (stage.includes("forecast")) return 5;
  return 0;
}

export default function NearLiveReview() {
  const { session } = useAuth();
  const token = session!.token;
  const videoRef = useRef<HTMLVideoElement>(null);
  const uploadRegisterKey = useRef(newIdempotencyKey());
  const uploadKey = useRef(newIdempotencyKey());
  const connectorKey = useRef(newIdempotencyKey());
  const [mode, setMode] = useState<InputMode>("live");
  const [source, setSource] = useState<LiveCctvSource | null>(null);
  const [registeredSources, setRegisteredSources] = useState<PublicSource[]>([]);
  const [run, setRun] = useState<NearLiveRun | null>(null);
  const [stage, setStage] = useState(0);
  const [candidates, setCandidates] = useState<PublicCandidate[]>([]);
  const [evidenceUrl, setEvidenceUrl] = useState<string | null>(null);
  const [evidenceFor, setEvidenceFor] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [reanalyzing, setReanalyzing] = useState(false);
  const [busyCandidate, setBusyCandidate] = useState<string | null>(null);
  const [predictionWindow, setPredictionWindow] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [operationNote, setOperationNote] = useState<string | null>(null);
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewRevision, setPreviewRevision] = useState(0);
  const [demoCaptureAvailability, setDemoCaptureAvailability] = useState<
    "loading" | "enabled" | "disabled"
  >("loading");

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadDuration, setUploadDuration] = useState<number | null>(null);
  const [uploadPreview, setUploadPreview] = useState<string | null>(null);
  const [uploadConsent, setUploadConsent] = useState(false);
  const [uploadSourceName, setUploadSourceName] = useState("Uploaded road recording");
  const [uploadSourceId, setUploadSourceId] = useState<string | null>(null);

  const [connectorName, setConnectorName] = useState("Operations camera");
  const [connectorTransport, setConnectorTransport] = useState<"hls" | "rtsp" | "onvif">("rtsp");
  const [connectorSecretId, setConnectorSecretId] = useState("");

  const refreshSources = async () => {
    const listing = await api.sources(token);
    setRegisteredSources(listing.items);
  };

  useEffect(() => {
    setRun(null);
    setStage(0);
    setCandidates([]);
    setError(null);
    setPreviewState("idle");
    setDemoCaptureAvailability("loading");
    void api.ready()
      .then((readiness) => {
        setDemoCaptureAvailability(
          readiness.near_live_capture === "allowlisted_hls" ? "enabled" : "disabled",
        );
      })
      .catch(() => setDemoCaptureAvailability("disabled"));
    void Promise.all([api.liveCctv(token), api.sources(token)])
      .then(([liveSource, sources]) => {
        setSource(liveSource);
        setRegisteredSources(sources.items);
      })
      .catch((caught) => {
        setError(caught instanceof Error ? caught.message : "Source controls are unavailable");
      });
  }, [token, session?.activeTenantId]);

  useEffect(() => {
    const video = videoRef.current;
    if (
      mode !== "live"
      || demoCaptureAvailability !== "enabled"
      || !video
      || !source?.playback_url
    ) {
      setPreviewState("idle");
      return;
    }
    setPreviewState("connecting");
    const markPlaying = () => setPreviewState("playing");
    const markWaiting = () => setPreviewState((current) =>
      current === "playing" ? "reconnecting" : current,
    );
    const markPaused = () => setPreviewState((current) =>
      current === "connecting" ? "paused" : current,
    );
    video.addEventListener("playing", markPlaying);
    video.addEventListener("waiting", markWaiting);
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source.playback_url;
      void video.play().catch(markPaused);
      return () => {
        video.removeEventListener("playing", markPlaying);
        video.removeEventListener("waiting", markWaiting);
        video.pause();
        video.removeAttribute("src");
        video.load();
      };
    }
    if (!Hls.isSupported()) {
      video.removeEventListener("playing", markPlaying);
      video.removeEventListener("waiting", markWaiting);
      setPreviewState("error");
      setError("This browser cannot play the HLS live feed.");
      return;
    }
    const hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
    let recoveryAttempts = 0;
    hls.loadSource(source.playback_url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => void video.play().catch(markPaused));
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      if (recoveryAttempts < 2 && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        recoveryAttempts += 1;
        setPreviewState("reconnecting");
        hls.startLoad();
        return;
      }
      if (recoveryAttempts < 2 && data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        recoveryAttempts += 1;
        setPreviewState("reconnecting");
        hls.recoverMediaError();
        return;
      }
      setPreviewState("error");
      setError("The public camera preview disconnected. The bounded server capture may be retried separately.");
    });
    return () => {
      video.removeEventListener("playing", markPlaying);
      video.removeEventListener("waiting", markWaiting);
      hls.destroy();
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [demoCaptureAvailability, mode, previewRevision, source?.playback_url]);

  useEffect(() => () => {
    if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
  }, [evidenceUrl]);

  useEffect(() => () => {
    if (uploadPreview) URL.revokeObjectURL(uploadPreview);
  }, [uploadPreview]);

  useEffect(() => {
    if (!run?.run_id || stage >= 4) return;
    const timer = window.setInterval(async () => {
      try {
        if (!run.asset_id) {
          const captureRun = await api.ingestionRun(token, run.run_id);
          setRun(captureRun);
          setStage(operationRank(captureRun.stage));
          if (captureRun.state === "failed") {
            setError(`Processing stopped: ${captureRun.error_code ?? "capture failure"}`);
          }
          return;
        }
        const listing = await api.ingestionRuns(token, 50);
        const related = listing.items.filter((item) => item.asset_id === run.asset_id);
        const latest = related.sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
        if (latest) {
          setRun(latest);
          setStage(Math.max(...related.map((item) => operationRank(item.stage)), 0));
          if (latest.state === "failed") {
            setError(`Processing stopped: ${latest.error_code ?? "worker failure"}`);
          } else {
            setError((current) => current?.startsWith("Processing stopped:") ? null : current);
          }
        }
        const analysisComplete = related.some(
          (item) => item.state === "completed" && item.stage.includes("analy"),
        );
        if (analysisComplete) {
          const listingCandidates = await api.candidates(token);
          setCandidates(listingCandidates.items.filter((item) => item.asset_id === run.asset_id));
          setStage(4);
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not refresh durable workers");
      }
    }, 1800);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.asset_id, stage, token]);

  const canManageSources = session?.role === "tenant_admin" || session?.role === "platform_operator";
  const canAnalyze = source?.analysis_mode === "reka_vision";
  const demoCaptureEnabled = demoCaptureAvailability === "enabled";
  const liveSourceAvailable = source?.status === "live" && Boolean(source.playback_url);
  const canCaptureLive = canManageSources && canAnalyze && demoCaptureEnabled && liveSourceAvailable;
  const pipelineRunning = run?.state === "queued" || run?.state === "running" || run?.state === "retry";
  const sourceBusy = capturing || uploading || pipelineRunning;
  const liveActionLabel = sourceBusy
    ? "Analysis in progress…"
    : demoCaptureAvailability === "loading"
      ? "Checking capture policy…"
      : canCaptureLive
        ? "Analyze next 12 seconds"
        : demoCaptureEnabled && source?.status === "unavailable"
          ? "Public camera unavailable"
          : "Public demo capture disabled";
  const simulationActionLabel = sourceBusy
    ? "Analysis in progress…"
    : demoCaptureAvailability === "loading"
      ? "Checking capture policy…"
      : demoCaptureEnabled
        ? "Run simulated analysis"
        : "Generated demo capture disabled";
  const canReanalyze =
    canAnalyze
    && (session?.role === "tenant_admin" || session?.role === "platform_operator")
    && run?.state === "failed"
    && run.stage.includes("analy");
  const statusText = useMemo(() => {
    if (capturing) return mode === "simulated" ? "Generating simulation…" : "Capturing…";
    if (uploading) return "Securing upload…";
    if (predictionWindow) return "Forecast updated";
    if (stage >= 4 && candidates.length === 0) return "Analysis complete — no incidents proposed";
    if (run) return run.stage.replaceAll("_", " ");
    if (operationNote) return operationNote;
    if (mode !== "upload" && demoCaptureAvailability === "disabled") {
      return "Production demo capture disabled";
    }
    return "Ready for input";
  }, [candidates.length, capturing, demoCaptureAvailability, mode, operationNote, predictionWindow, run, stage, uploading]);

  const stages = useMemo(() => [
    mode === "upload" ? "Media intake" : mode === "simulated" ? "Simulation" : "Live capture",
    "Reka upload",
    "Vision indexing",
    "Candidate analysis",
    "Human review",
    "Future prediction",
  ], [mode]);

  function beginRun() {
    setError(null);
    setOperationNote(null);
    setCandidates([]);
    setPredictionWindow(null);
    setStage(0);
  }

  async function startCapture() {
    if (!canManageSources) {
      setError("Tenant administrator access is required to start a live capture.");
      return;
    }
    if (!demoCaptureEnabled || !source || source.status !== "live") {
      setError("Public demonstration capture is disabled in production. Use an authorized upload or mobile clip.");
      return;
    }
    beginRun();
    setCapturing(true);
    try {
      const next = await api.startNearLiveCapture(token, source.source_key, 12);
      setRun(next);
      setStage(1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Live capture could not start");
    } finally {
      setCapturing(false);
    }
  }

  async function startSimulation() {
    if (!demoCaptureEnabled) {
      setError("Generated demo capture is disabled in production. Use an authorized upload or mobile clip.");
      return;
    }
    beginRun();
    setCapturing(true);
    try {
      const next = await api.startSimulatedCapture(token, 8);
      setRun(next);
      setStage(1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Simulation could not start");
    } finally {
      setCapturing(false);
    }
  }

  async function selectUploadFile(selected: File | null) {
    setError(null);
    setUploadSourceId(null);
    if (uploadPreview) URL.revokeObjectURL(uploadPreview);
    setUploadPreview(null);
    if (!selected) {
      setUploadFile(null);
      setUploadDuration(null);
      return;
    }
    if ((!selected.type.startsWith("video/mp4") && !selected.name.toLowerCase().endsWith(".mp4")) || selected.size > MAX_UPLOAD_BYTES) {
      setUploadFile(null);
      setUploadDuration(null);
      setError(selected.size > MAX_UPLOAD_BYTES ? "MP4 exceeds the 64 MB demo intake bound." : "Only MP4 recordings are accepted.");
      return;
    }
    const preview = URL.createObjectURL(selected);
    try {
      const duration = await new Promise<number>((resolve, reject) => {
        const probe = document.createElement("video");
        probe.preload = "metadata";
        probe.onloadedmetadata = () => resolve(probe.duration);
        probe.onerror = () => reject(new Error("metadata"));
        probe.src = preview;
      });
      if (!Number.isFinite(duration) || duration < 1 || duration > 600) throw new Error("duration");
      setUploadFile(selected);
      setUploadDuration(duration);
      setUploadPreview(preview);
      setUploadSourceName(selected.name.replace(/\.mp4$/i, "") || "Uploaded road recording");
    } catch {
      URL.revokeObjectURL(preview);
      setUploadFile(null);
      setUploadDuration(null);
      setError("The MP4 must contain 1 second to 10 minutes of readable video.");
    }
  }

  async function startUpload() {
    if (!uploadFile || !uploadDuration || !uploadConsent || !uploadSourceName.trim()) return;
    beginRun();
    setUploading(true);
    try {
      let recordedSourceId = uploadSourceId;
      if (!recordedSourceId) {
        const recordedSource = await api.createRecordedSource(
          token,
          {
            name: uploadSourceName.trim(),
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
            registered_location_id: DEMO_LOCATION_ID,
            retention_policy_days: 7,
          },
          uploadRegisterKey.current,
        );
        recordedSourceId = recordedSource.source_id;
        setUploadSourceId(recordedSourceId);
        uploadRegisterKey.current = newIdempotencyKey();
        void refreshSources();
      }
      const capturedEnd = new Date();
      const next = await api.uploadVideo(
        token,
        {
          sourceId: recordedSourceId,
          capturedStart: new Date(capturedEnd.getTime() - uploadDuration * 1000).toISOString(),
          capturedEnd: capturedEnd.toISOString(),
          consentConfirmed: true,
          file: uploadFile,
        },
        uploadKey.current,
      );
      uploadKey.current = newIdempotencyKey();
      setRun(next);
      setStage(1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Video upload could not start");
    } finally {
      setUploading(false);
    }
  }

  async function connectLiveSource() {
    if (!connectorName.trim() || !UUID_PATTERN.test(connectorSecretId)) return;
    setConnecting(true);
    setError(null);
    setOperationNote(null);
    try {
      const connected = await api.createLiveSource(
        token,
        {
          name: connectorName.trim(),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
          registered_location_id: DEMO_LOCATION_ID,
          retention_policy_days: 7,
          connection_secret_id: connectorSecretId,
          transport: connectorTransport,
        },
        connectorKey.current,
      );
      connectorKey.current = newIdempotencyKey();
      setConnectorSecretId("");
      setOperationNote(`${connected.name} registered for edge capture`);
      await refreshSources();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Live source could not be registered");
    } finally {
      setConnecting(false);
    }
  }

  async function reanalyze() {
    if (!run || !canReanalyze) return;
    setReanalyzing(true);
    setError(null);
    try {
      const fresh = await api.reanalyzeRun(token, run.run_id, newIdempotencyKey());
      setRun(fresh);
      setCandidates([]);
      setStage(3);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Re-analysis could not start");
    } finally {
      setReanalyzing(false);
    }
  }

  async function playEvidence(candidate: PublicCandidate) {
    setError(null);
    try {
      const blob = await api.candidateEvidence(token, candidate.detection_id);
      if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
      setEvidenceUrl(URL.createObjectURL(blob));
      setEvidenceFor(candidate.detection_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Evidence could not be authorized");
    }
  }

  async function decide(candidate: PublicCandidate, decision: "confirmed" | "rejected") {
    setBusyCandidate(candidate.detection_id);
    setError(null);
    try {
      await api.reviewCandidate(
        token,
        candidate.detection_id,
        decision === "confirmed"
          ? { decision, confirmed_category: candidate.proposed_category === "unmapped" ? "other" : candidate.proposed_category, rejection_reason: null }
          : { decision, confirmed_category: null, rejection_reason: "insufficient_evidence" },
        newIdempotencyKey(),
      );
      setCandidates((current) => current.map((item) =>
        item.detection_id === candidate.detection_id ? { ...item, review_status: decision } : item,
      ));
      if (decision === "confirmed") {
        const forecast = await api.refreshDemoForecasts(token);
        window.localStorage.setItem(`demo-forecast-window:${session!.activeTenantId}`, forecast.window_start);
        window.localStorage.setItem(`demo-forecast-category:${session!.activeTenantId}`, candidate.proposed_category === "unmapped" ? "other" : candidate.proposed_category);
        setPredictionWindow(forecast.window_start);
        setStage(5);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The final review could not be saved");
    } finally {
      setBusyCandidate(null);
    }
  }

  return (
    <section className="live-operations" id="live-operations">
      <div className="live-heading">
        <div>
          <p className="eyebrow">Restricted media intake · real model path</p>
          <h1>Choose. Analyze. <span>Verify.</span> Predict.</h1>
          <p>Every source enters the same tenant-scoped Reka and human-review workflow.</p>
        </div>
      </div>

      <div className="source-mode-rail" role="tablist" aria-label="Video input method">
        {MODE_OPTIONS.map((option) => (
          <button key={option.id} type="button" role="tab" aria-selected={mode === option.id} className={mode === option.id ? "active" : ""} disabled={sourceBusy} onClick={() => { setMode(option.id); setError(null); setOperationNote(null); }}>
            <span>{option.index}</span><strong>{option.label}</strong><small>{option.detail}</small>
          </button>
        ))}
      </div>

      <div className="custody-rail" aria-label="Video analysis workflow">
        {stages.map((label, index) => (
          <div className={`custody-step${index <= stage ? " reached" : ""}`} key={label}>
            <span>{String(index + 1).padStart(2, "0")}</span><b>{label}</b>
          </div>
        ))}
      </div>

      <div className="operations-grid">
        {mode === "live" && (
          <article className="live-monitor" aria-labelledby="public-camera-name">
            <div className={`monitor-label ${liveSourceAvailable ? "is-live" : "is-offline"}`}>
              <i /> {liveSourceAvailable ? "PUBLIC FEED" : "FEED OFFLINE"} · <span id="public-camera-name">{source?.name ?? "Checking camera policy…"}</span>
            </div>
            {demoCaptureEnabled && source && liveSourceAvailable ? (
              <>
                <video
                  ref={videoRef}
                  autoPlay
                  muted
                  controls
                  playsInline
                  aria-label={`Live public camera preview: ${source.name}`}
                  aria-describedby="public-camera-attribution public-camera-boundary"
                />
                <div className="preview-state" role="status" aria-live="polite">
                  <span className={`preview-state-dot ${previewState}`} />
                  {previewState === "playing"
                    ? "Preview connected"
                    : previewState === "reconnecting"
                      ? "Reconnecting preview…"
                      : previewState === "paused"
                        ? "Preview ready — press play"
                        : previewState === "error"
                          ? "Preview unavailable"
                          : "Connecting preview…"}
                  {previewState === "error" && (
                    <button type="button" onClick={() => { setError(null); setPreviewRevision((value) => value + 1); }}>
                      Retry preview
                    </button>
                  )}
                </div>
              </>
            ) : (
              <div className="upload-empty" role="status">
                <span>OFF</span>
                <b>{demoCaptureAvailability === "loading" ? "Checking production capture policy" : "Public demo camera disabled in production"}</b>
              </div>
            )}
            <div className="public-source-facts" aria-label="Public camera details">
              <div><span>Source</span><strong>{source?.name ?? "Loading…"}</strong></div>
              <div><span>Feed state</span><strong>{source?.status ?? "checking"}</strong></div>
              <div><span>Analysis</span><strong>{canAnalyze ? "Reka Vision" : "unavailable"}</strong></div>
              <div><span>Capture bound</span><strong>12 seconds</strong></div>
            </div>
            {source?.limitations?.length ? (
              <details className="public-source-limitations">
                <summary>Feed limitations</summary>
                <ul>{source.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
              </details>
            ) : null}
            <div className="monitor-footer">
              <span id="public-camera-attribution">{source?.attribution ?? "Approved public traffic source"}</span>
              <span id="public-camera-boundary">Preview is context; only a bounded clip enters analysis</span>
            </div>
          </article>
        )}

        {mode === "upload" && <article className="media-intake-panel"><div className="monitor-label">RECORDED · TENANT AUTHORIZATION REQUIRED</div>{uploadPreview ? <video src={uploadPreview} controls muted playsInline aria-label="Selected video preview" /> : <div className="upload-empty" aria-hidden="true"><span>MP4</span><b>No recording selected</b></div>}<div className="intake-fields"><label className="file-drop compact"><input type="file" accept="video/mp4,.mp4" onChange={(event) => void selectUploadFile(event.target.files?.[0] ?? null)} />{uploadFile ? `${uploadFile.name} · ${uploadDuration?.toFixed(1)}s · ${(uploadFile.size / 1_048_576).toFixed(1)} MB` : "Choose MP4 · up to 64 MB"}</label><label>Source label<input value={uploadSourceName} maxLength={120} onChange={(event) => setUploadSourceName(event.target.value)} /></label><label className="authorization-check"><input type="checkbox" checked={uploadConsent} onChange={(event) => setUploadConsent(event.target.checked)} /><span>I confirm this tenant is authorized to process this recording.</span></label></div></article>}

        {mode === "simulated" && <article className="simulated-monitor" aria-label="Synthetic road simulation preview"><div className="monitor-label"><i /> SIMULATED · NO REAL PEOPLE OR EVENTS</div><div className="synthetic-sky" /><div className="synthetic-road"><span className="lane-mark lane-one" /><span className="lane-mark lane-two" /><span className="synthetic-car car-one" /><span className="synthetic-car car-two" /></div><div className="monitor-footer"><span>Generated locally</span><span>8-second deterministic segment</span></div></article>}

        <aside className="evidence-rail">
          <p className="eyebrow">Current operation</p><h2>{statusText}</h2><p>Durable worker path. Inputs remain restricted and tenant-scoped.</p>{run && <code>run {run.run_id.slice(0, 8)}</code>}
          {mode === "live" && <><button className="capture-button" type="button" onClick={startCapture} disabled={sourceBusy || !canCaptureLive}>{liveActionLabel}</button><details className="connector-config"><summary>Register tenant camera connector</summary><p>{demoCaptureEnabled ? "Reference a credential already stored by your deployment. Raw URLs and secrets never enter this browser." : "Registration stores only the tenant-scoped connector reference. Capture stays off until a pinned media proxy and worker are deployed."}</p><label>Source name<input value={connectorName} maxLength={120} onChange={(event) => setConnectorName(event.target.value)} /></label><label>Transport<select value={connectorTransport} onChange={(event) => setConnectorTransport(event.target.value as "hls" | "rtsp" | "onvif")}><option value="rtsp">RTSP</option><option value="onvif">ONVIF</option><option value="hls">HLS</option></select></label><label>Connection secret ID<input value={connectorSecretId} placeholder="00000000-0000-4000-8000-000000000000" onChange={(event) => setConnectorSecretId(event.target.value)} /></label><button className="review-action evidence" type="button" disabled={connecting || !UUID_PATTERN.test(connectorSecretId) || !connectorName.trim()} onClick={connectLiveSource}>{connecting ? "Registering…" : "Register connector"}</button></details>{registeredSources.filter((item) => item.mode === "live_camera").length > 0 && <small className="source-count">{registeredSources.filter((item) => item.mode === "live_camera").length} live source(s) registered</small>}</>}
          {mode === "upload" && <button className="capture-button" type="button" onClick={startUpload} disabled={sourceBusy || !canAnalyze || !uploadFile || !uploadDuration || !uploadConsent || !uploadSourceName.trim()}>{uploading || pipelineRunning ? "Analysis in progress…" : "Upload & analyze video"}</button>}
          {mode === "simulated" && <><button className="capture-button" type="button" onClick={startSimulation} disabled={sourceBusy || !canAnalyze || !demoCaptureEnabled}>{simulationActionLabel}</button><p className="simulation-note">Simulation validates transport and review mechanics. It is not evidence of model accuracy.</p></>}
          {mode !== "upload" && demoCaptureAvailability === "disabled" && <div className="pipeline-notice warning">Production capture is closed on this host. The authorized MP4 and mobile-camera paths remain available.</div>}
          {!canAnalyze && source && <div className="pipeline-notice warning">Real Reka Vision is not configured. Analysis is disabled; this demo never substitutes fabricated detections.</div>}
          {error && <div className="pipeline-notice error" role="alert">{error}</div>}
          {canReanalyze && <button className="review-action evidence" type="button" disabled={reanalyzing} onClick={reanalyze}>{reanalyzing ? "Queueing re-analysis…" : "Re-analyze retained segment"}</button>}
          {evidenceUrl && <div className="evidence-player"><span>Authorized captured evidence</span><video src={evidenceUrl} controls autoPlay muted playsInline /></div>}
        </aside>
      </div>

      <div className="candidate-header"><div><span>Reviewer-only queue</span><h2>Unconfirmed candidate incidents</h2></div><b>{candidates.length.toString().padStart(2, "0")}</b></div>
      <div className="candidate-list">
        {stage >= 4 && candidates.length === 0 && <div className="candidate-empty">Analysis complete. Reka proposed no qualifying incident in this segment.</div>}
        {stage < 4 && <div className="candidate-empty">Candidates appear after analysis.</div>}
        {candidates.map((candidate) => (
          <motion.article className="candidate-row" key={candidate.detection_id} layout>
            <div className="candidate-primary">
              <span className="candidate-flag">Unconfirmed · human decision required</span>
              <h3>{candidate.event_type.replaceAll("_", " ")}</h3>
              <p>{candidate.description}</p>
              <small>
                {candidate.proposed_category.replaceAll("_", " ")} · {new Date(candidate.occurred_at).toUTCString()}
              </small>
            </div>
            <div className="candidate-confidence">
              <span>Reka observation confidence</span>
              <strong>{Math.round(candidate.confidence * 100)}%</strong>
              <small>Not probability of crime</small>
            </div>
            <div className="candidate-actions">
              <button type="button" className="review-action evidence" onClick={() => playEvidence(candidate)}>
                {evidenceFor === candidate.detection_id ? "Replay evidence" : "View evidence"}
              </button>
              {candidate.review_status === "awaiting_review" ? (
                <>
                  <button type="button" className="review-action reject" disabled={Boolean(busyCandidate)} onClick={() => decide(candidate, "rejected")}>Reject</button>
                  <button type="button" className="review-action confirm" disabled={Boolean(busyCandidate)} onClick={() => decide(candidate, "confirmed")}>Confirm &amp; predict</button>
                </>
              ) : (
                <span className={`decision ${candidate.review_status}`}>Final: {candidate.review_status}</span>
              )}
            </div>
          </motion.article>
        ))}
      </div>
      {predictionWindow && <div className="prediction-unlocked"><div><span>Future window published</span><strong>{new Date(predictionWindow).toUTCString()}</strong></div><a href="#/console/map">Open updated crime-prediction map →</a></div>}
      <p className="review-limitation">Aggregate risk only · human confirmation required.</p>
    </section>
  );
}
