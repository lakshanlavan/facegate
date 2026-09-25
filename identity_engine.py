from __future__ import annotations

import os
import threading

import cv2
import numpy as np
from pathlib import Path
from typing import Any, Dict, Generator, List, Tuple


class IdentityEngine:
    """
    Phase 2 identity / similarity engine.

    Faces:
        InsightFace ArcFace

    Products:
        OpenCLIP + FAISS-style store with top-k + margin selection
    """

    _face_app = None

    @classmethod
    def _get_face_app(cls):
        """Load InsightFace FaceAnalysis once and reuse it."""
        if cls._face_app is None:
            try:
                from insightface.app import FaceAnalysis

                cls._face_app = FaceAnalysis(
                    name="buffalo_sc",
                    providers=["CPUExecutionProvider"],
                )
                cls._face_app.prepare(ctx_id=0, det_size=(320, 320))
                print("[IDENTITY] InsightFace ArcFace loaded")
            except Exception as e:
                print(f"[IDENTITY] InsightFace load failed: {e}")
                cls._face_app = None
        return cls._face_app

    def __init__(self, scenario: Dict[str, Any]) -> None:
        self.backend = scenario.get("hardware_backend", "cpu")
        self.model_path = scenario.get(
            "model_path",
            "models/pretrained/identity/mobilenet_v2_embedding.tflite",
        )
        self.store_type = scenario.get("identity_store", "faces")
        self.match_threshold = float(scenario.get("identity_threshold", 0.40))
        self.threshold = float(scenario.get("confidence_threshold", 0.50))

        # Product-only settings
        self.top_k = int(scenario.get("top_k", 5))
        self.min_score = float(scenario.get("product_min_score", 0.28))
        self.min_margin = float(scenario.get("product_min_margin", 0.03))

        # Load face app once
        self._get_face_app()

        # Face store
        self._enrolled: Dict[str, List[np.ndarray]] = {}
        self._enrolled_lock = threading.RLock()
        self._face_index_names: List[str] = []
        self._face_index_matrix: np.ndarray | None = None

        enrolments_dir = scenario.get("enrolments_dir", "enrolments/shared")
        self.enrolments_dir = Path(enrolments_dir) / self.store_type
        self.enrolments_dir.mkdir(parents=True, exist_ok=True)

        self._load_enrolled()
        self._rebuild_face_index()

        # Product store
        if self.store_type == "products":
            from server.embedding_extractor_openclip import EmbeddingExtractor
            from server.enrolment_store import EnrolmentStore

            self.extractor = EmbeddingExtractor(
                model_name=scenario.get("openclip_model_name", "ViT-B-32"),
                pretrained=scenario.get(
                    "openclip_pretrained",
                    "laion2b_s34b_b79k",
                ),
                device="cuda" if self.backend == "cuda" else "cpu",
            )

            self.store = EnrolmentStore(
                store_name=self.store_type,
                base_dir=enrolments_dir,
            )
        else:
            self.extractor = None
            self.store = None

        # Optional product Excel lookup
        self.excel_lookup = None
        if self.store_type == "products":
            try:
                from server.excel_lookup import ExcelLookup

                excel_path = scenario.get(
                    "excel_db",
                    "product_db/Products page - Data base - All Categories..xlsx",
                )
                self.excel_lookup = ExcelLookup(excel_path)
                print("[IDENTITY] Excel DB cached at init")
            except Exception as e:
                print(f"[IDENTITY] Excel cache failed: {e}")

        enrolled_count = (
            len(self._enrolled)
            if self.store_type == "faces"
            else (self.store.count() if self.store else 0)
        )

        print(
            f"[IDENTITY] Engine ready\n"
            f"  store       : {self.store_type}\n"
            f"  enrolled    : {enrolled_count} identities\n"
            f"  threshold   : {self.match_threshold}\n"
            f"  engine      : "
            f"{'InsightFace ArcFace' if self.store_type == 'faces' else 'OpenCLIP FAISS'}"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Face enrolment storage
    # ──────────────────────────────────────────────────────────────────────────

    def _enrolled_file(self) -> Path:
        return self.enrolments_dir / "arcface_embeddings.npz"

    def _load_enrolled(self) -> None:
        """Load saved ArcFace embeddings from disk."""
        f = self._enrolled_file()
        if f.exists() and self.store_type == "faces":
            try:
                data = np.load(str(f), allow_pickle=True)
                with self._enrolled_lock:
                    self._enrolled.clear()
                    for name in data.files:
                        self._enrolled[name] = [
                            np.asarray(e, dtype=np.float32) for e in list(data[name])
                        ]
                print(
                    f"[ENROLMENT] Loaded {len(self._enrolled)} face enrolments from ArcFace store"
                )
            except Exception as e:
                print(f"[IDENTITY] Failed to load enrolments: {e}")

    def _save_enrolled(self) -> None:
        """Atomically save ArcFace embeddings to disk and refresh the fast index."""
        if self.store_type != "faces":
            return

        with self._enrolled_lock:
            save_dict = {
                name: np.asarray(embs, dtype=np.float32)
                for name, embs in self._enrolled.items()
            }

        target = self._enrolled_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")

        # Atomic replace protects the main embedding file from corruption if
        # power is interrupted during enrolment/rename/remove.
        np.savez(str(tmp), **save_dict)
        tmp_npz = Path(str(tmp) + ".npz") if not str(tmp).endswith(".npz") else tmp
        os.replace(str(tmp_npz), str(target))
        self._rebuild_face_index()

    def _rebuild_face_index(self) -> None:
        """
        Build a dense matrix for fast face matching.

        Old behaviour: Python loop over every member and every embedding.
        New behaviour: one NumPy matrix multiplication for all embeddings.
        This matters when the gym reaches 1000+ members × 3 captures.

        Thread-safety: take a shallow snapshot under the lock, build the
        matrix WITHOUT holding the lock (so detection never stalls during
        an enrol/remove), then swap the new matrix in atomically.
        """
        if self.store_type != "faces":
            return

        # ── Step 1: snapshot enrolled dict under lock (fast O(n) copy) ───────
        with self._enrolled_lock:
            snapshot = {
                name: list(embs)
                for name, embs in self._enrolled.items()
            }

        # ── Step 2: build matrix WITHOUT holding the lock ─────────────────────
        names: List[str] = []
        vectors: List[np.ndarray] = []

        for name, emb_list in snapshot.items():
            for emb in emb_list:
                arr = np.asarray(emb, dtype=np.float32).reshape(-1)
                if arr.size == 0:
                    continue
                norm = float(np.linalg.norm(arr))
                if norm > 0:
                    arr = arr / norm
                names.append(name)
                vectors.append(arr)

        new_matrix = np.vstack(vectors).astype(np.float32, copy=False) if vectors else None

        # ── Step 3: atomic swap under lock (O(1) pointer replace) ────────────
        with self._enrolled_lock:
            self._face_index_matrix = new_matrix
            self._face_index_names  = names if vectors else []

        if vectors:
            print(f"[IDENTITY] Face index ready: {len(vectors)} embeddings / {len(set(names))} identities")

    # ──────────────────────────────────────────────────────────────────────────
    # Face analysis / matching
    # ──────────────────────────────────────────────────────────────────────────

    def analyse_face(self, frame: np.ndarray):
        """
        Run InsightFace once on the frame.
        Returns largest face data or None.
        """
        app = self._get_face_app()
        if app is None:
            return None

        try:
            faces = app.get(frame)
        except Exception as e:
            print(f"[IDENTITY] InsightFace error: {e}")
            return None

        if not faces:
            return None

        best = max(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
        )

        x1, y1, x2, y2 = best.bbox.astype(int).tolist()

        return {
            "face": best,
            "embedding": best.normed_embedding,
            "bbox": [x1, y1, x2, y2],
            "faces_count": len(faces),
        }

    def _match_face(self, embedding: np.ndarray, threshold: float):
        """
        Compare embedding against enrolled faces using cosine similarity.
        Uses a vectorized index for stable low latency with 1000+ members.
        Returns (name, confidence) or ("Unknown", 0.0).
        """
        with self._enrolled_lock:
            matrix = self._face_index_matrix
            names = list(self._face_index_names)

        if matrix is None or matrix.size == 0 or not names:
            return "Unknown", 0.0

        query = np.asarray(embedding, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(query))
        if norm > 0:
            query = query / norm

        try:
            scores = matrix @ query
        except ValueError:
            # If old/corrupt embeddings have unexpected dimensions, rebuild once
            # and fail closed rather than blocking the detection loop.
            print("[IDENTITY] Face index dimension mismatch — rebuilding")
            self._rebuild_face_index()
            return "Unknown", 0.0

        best_idx = int(np.argmax(scores))
        best_sim = float(scores[best_idx])
        best_name = names[best_idx]

        if best_sim >= threshold:
            return best_name, best_sim

        return "Unknown", best_sim

    def match_face_embedding(
        self,
        embedding: np.ndarray,
        threshold: float | None = None,
    ):
        if threshold is None:
            threshold = self.match_threshold
        return self._match_face(embedding, threshold)

    # ──────────────────────────────────────────────────────────────────────────
    # Product candidate selector
    # ──────────────────────────────────────────────────────────────────────────

    def _select_best_product_candidate(self, candidates: List[Dict[str, Any]]):
        """
        Accept only if:
        - top1 score >= min_score
        - top1 - top2 >= min_margin
        """
        if not candidates:
            return None

        top1 = candidates[0]
        top2 = candidates[1] if len(candidates) > 1 else None

        score1 = float(top1.get("score", 0.0))
        score2 = float(top2.get("score", 0.0)) if top2 else -1.0

        if score1 < self.min_score:
            print(
                f"[PRODUCT] Reject: top1 score too low "
                f"({score1:.4f} < {self.min_score:.4f})"
            )
            return None

        if top2 is not None and (score1 - score2) < self.min_margin:
            print(
                f"[PRODUCT] Reject: margin too small "
                f"({score1:.4f} - {score2:.4f} < {self.min_margin:.4f})"
            )
            return None

        return top1

    # ──────────────────────────────────────────────────────────────────────────
    # Main inference
    # ──────────────────────────────────────────────────────────────────────────

    def run_frame(self, frame: Any, scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Logic:
          Products:
            embedding -> top-k candidates -> top1/top2 margin check -> Excel display
          Faces:
            Face detected + enrolled match  -> name + confidence
            Face detected + no match        -> Unknown Person
            No face detected                -> []
        """
        threshold = float(scenario.get("identity_threshold", self.match_threshold))

        # Product flow
        if self.store_type == "products":
            embedding = self.extractor.extract(frame)

            candidates = self.store.match_top_k(
                embedding,
                k=self.top_k,
            )

            best = self._select_best_product_candidate(candidates)
            if best is None:
                return []

            name = best["name"]
            confidence = float(best["score"])

            display_name = name
            if self.excel_lookup is not None:
                try:
                    from server.excel_lookup import ExcelLookup

                    product = self.excel_lookup.lookup(name)
                    if product.get("found"):
                        display_name = ExcelLookup.format_screen(product)
                        print(f"[PRODUCT] {name} -> {product.get('name', '')}")
                    else:
                        print(f"[PRODUCT] {name} not found in Excel sheet")
                except Exception as e:
                    print(f"[IDENTITY] excel_lookup error: {e}")

            return [
                {
                    "class_name": display_name,
                    "confidence": round(confidence, 4),
                    "bbox": [],
                    "candidates": candidates,
                }
            ]

        # Face flow
        face_data = self.analyse_face(frame)
        if face_data is None:
            return []

        embedding = face_data["embedding"]
        bbox = face_data["bbox"]

        name, confidence = self.match_face_embedding(embedding, threshold)

        if name != "Unknown":
            print(f"[IDENTITY] match='{name}' similarity={confidence:.4f}")

        if name == "Unknown":
            return [
                {
                    "class_name": "Unknown Person",
                    "confidence": 0.0,
                    "bbox": bbox,
                }
            ]

        return [
            {
                "class_name": name,
                "confidence": round(confidence, 4),
                "bbox": bbox,
            }
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # Enrolment
    # ──────────────────────────────────────────────────────────────────────────

    def enrol_from_images(self, name: str, image_paths: List[str]) -> int:
        """Enrol from image file paths."""
        if self.store_type == "products":
            embeddings = []
            for path in image_paths:
                try:
                    embeddings.append(self.extractor.extract_from_file(path))
                except Exception as e:
                    print(f"[IDENTITY] Skipping {path}: {e}")

            if not embeddings:
                raise ValueError(f"No valid images for '{name}'")

            self.store.enrol_many(name, embeddings)
            return self.store.count()

        new_embeddings = []
        for path in image_paths:
            try:
                img = cv2.imread(str(path))
                if img is None:
                    continue

                face_data = self.analyse_face(img)
                if face_data is not None:
                    new_embeddings.append(face_data["embedding"])
                else:
                    print(f"[IDENTITY] No face found in {path} - skipping")
            except Exception as e:
                print(f"[IDENTITY] Skipping {path}: {e}")

        if not new_embeddings:
            raise ValueError(f"No valid face images for '{name}'")

        if name not in self._enrolled:
            self._enrolled[name] = []
        self._enrolled[name].extend(new_embeddings)
        self._save_enrolled()
        print(
            f"[IDENTITY] '{name}' enrolled - {len(new_embeddings)} face embeddings saved"
        )
        return len(self._enrolled)

    def enrol_from_frame(self, name: str, frame: Any) -> int:
        if self.store_type == "products":
            emb = self.extractor.extract(frame)
            self.store.enrol(name, emb)
            return self.store.count()

        face_data = self.analyse_face(frame)
        if face_data is None:
            raise ValueError("No face detected in frame")

        emb = face_data["embedding"]

        if name not in self._enrolled:
            self._enrolled[name] = []
        self._enrolled[name].append(emb)
        self._save_enrolled()
        return len(self._enrolled)

    def list_enrolled(self) -> List[str]:
        if self.store_type == "faces":
            return list(self._enrolled.keys())
        return self.store.list_enrolled()

    def remove_enrolled(self, name: str) -> bool:
        if self.store_type == "faces":
            if name in self._enrolled:
                del self._enrolled[name]
                self._save_enrolled()
                print(f"[IDENTITY] Removed: {name}")
                return True
            return False
        return self.store.remove(name)

    def rename_enrolled(self, old_name: str, new_name: str) -> bool:
        """
        Rename a face embedding entry in the in-process store and persist
        the change atomically to arcface_embeddings.npz.

        Operation:
            1. Verify old_name exists in _enrolled
            2. Verify new_name does NOT already exist (would overwrite embeddings)
            3. Move embeddings list under new_name key
            4. Delete old_name key
            5. Save to disk (atomic npz rewrite)

        Returns:
            True  — renamed successfully
            False — old_name not found in embedding store

        Raises:
            ValueError — new_name already exists in the embedding store
                         (caller must remove it first if intentional overwrite)

        Thread safety:
            _enrolled is a plain dict mutated only in the detection loop
            (single thread) and in enrol/remove/rename admin operations
            (one at a time via HTTP). No additional lock needed here —
            the FastAPI route layer serialises admin calls naturally.
            The npz file write is the only I/O; it replaces the file atomically
            via numpy's savez (writes to a temp file then renames on most OSes).

        Performance:
            npz rewrite is O(n_staff * 3_embeddings * embedding_dim).
            With 50 staff × 3 × 512 floats = ~300 KB rewrite — completes in
            < 5 ms on OptiPlex SSD. Called only on admin rename action,
            never on the detection hot path.
        """
        if self.store_type != "faces":
            # Product store — not applicable, caller handles via store.rename()
            return False

        old_name = old_name.strip()
        new_name = new_name.strip()

        if not old_name or not new_name:
            raise ValueError("Names cannot be empty")

        if old_name == new_name:
            # No-op — treat as success so the caller doesn't need to special-case this
            return True

        if old_name not in self._enrolled:
            return False

        if new_name in self._enrolled:
            raise ValueError(
                f"Embedding key '{new_name}' already exists. "
                f"Remove it first before renaming '{old_name}' to '{new_name}'."
            )

        # Move embeddings under new key, remove old key
        self._enrolled[new_name] = self._enrolled.pop(old_name)

        # Persist to disk — detection loop will pick up new name on next match
        self._save_enrolled()

        print(f"[IDENTITY] Embeddings renamed: '{old_name}' → '{new_name}'")
        return True

    def count(self) -> int:
        if self.store_type == "faces":
            return len(self._enrolled)
        return self.store.count()

    # ──────────────────────────────────────────────────────────────────────────
    # Source runner
    # ──────────────────────────────────────────────────────────────────────────

    def run_source(
        self,
        source: str,
        scenario: Dict[str, Any],
    ) -> Generator[Tuple[int, Any, List[Dict[str, Any]]], None, None]:
        source_type = scenario.get("camera_source", {}).get("type", "video")
        is_video = source_type == "video"
        frame_id = 0
        loop_count = 0
        avg_frame = None
        warmup = 0
        WARMUP_FRAMES = 30

        while True:
            cap = self._open_source(source)
            if not cap.isOpened():
                raise RuntimeError(f"[IDENTITY] Cannot open source: {source}")

            if is_video and loop_count > 0:
                print(f"[IDENTITY] Video loop {loop_count} | frames: {frame_id}")

            try:
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        if is_video:
                            loop_count += 1
                            break
                        print("[IDENTITY] Stream ended.")
                        return

                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    gray = cv2.GaussianBlur(gray, (21, 21), 0)

                    if avg_frame is None:
                        avg_frame = gray.astype(np.float32)
                        frame_id += 1
                        continue

                    if warmup < WARMUP_FRAMES:
                        cv2.accumulateWeighted(gray, avg_frame, 0.5)
                        warmup += 1
                        frame_id += 1
                        if warmup == WARMUP_FRAMES:
                            print("[IDENTITY] Background baseline ready.")
                        continue

                    cv2.accumulateWeighted(gray, avg_frame, 0.01)

                    if frame_id % 10 == 0:
                        detections = self.run_frame(frame, scenario)
                    else:
                        detections = []

                    yield frame_id, frame, detections
                    frame_id += 1
            finally:
                cap.release()

            if not is_video:
                break

    def _open_source(self, source: str):
        if source.startswith("rtsp://"):
            return cv2.VideoCapture(source)
        if str(source).isdigit():
            return cv2.VideoCapture(int(source))
        return cv2.VideoCapture(str(Path(source)))

    # ──────────────────────────────────────────────────────────────────────────
    # Annotation
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def annotate_frame(frame: Any, detections: List[Dict[str, Any]]) -> Any:
        annotated = frame.copy()

        # ── Colour palette (BGR) ──────────────────────────────────────────────
        # IMPORTANT:
        # This mapping only controls drawing colours/labels.
        # Recognition, payment, relay, DB and gate decision logic are unchanged.
        COLOURS = {
            # Current CodeGloFix access states from face_gate.py / gym_access.py
            "access_granted":               (34, 197, 94),   # green
            "access_granted_expiring":      (0, 158, 245),   # amber/orange
            "access_denied_expired":        (40, 40, 220),   # red
            "access_denied_inactive":       (40, 40, 220),   # red
            "access_denied_unknown":        (40, 40, 220),   # red
            "access_denied_emergency_lock": (40, 40, 220),   # red

            # Gate validation states
            "too_far":    (0, 165, 255),    # amber — guidance, not denial
            "off_centre": (0, 165, 255),    # amber — guidance, not denial
            "multi_face": (40, 40, 220),    # red
            "no_face":    (180, 180, 180),  # grey
            "cooldown":   (34, 197, 94),    # green

            # Legacy compatibility states from an earlier version
            "recognised":      (34, 197, 94),
            "already_present": (34, 197, 94),
            "unknown":         (40, 40, 220),
        }
        DEFAULT_COLOUR = (180, 180, 180)

        # OpenCV's built-in Hershey fonts are ASCII-only. Convert any Unicode
        # symbols before measuring/drawing text; otherwise symbols such as
        # em dashes can appear as "???" on the video overlay.
        def _cv_safe_text(text) -> str:
            text = str(text)
            replacements = {
                "—": "-",
                "–": "-",
                "−": "-",
                "·": "-",
                "•": "-",
                "→": "->",
                "←": "<-",
                "↔": "<->",
                "✓": "OK",
                "✔": "OK",
                "✅": "OK",
                "⚠": "!",
                "❌": "X",
            }
            for old, replacement in replacements.items():
                text = text.replace(old, replacement)
            return text.encode("ascii", "ignore").decode("ascii")

        # ── Label text per state ─────────────────────────────────────────────
        def get_label(gate_state, name, conf):
            if gate_state in (
                "access_granted",
                "access_granted_expiring",
                "recognised",
                "already_present",
                "cooldown",
            ):
                return f"  {name}  "
            elif gate_state == "too_far":
                return "  Move closer  "
            elif gate_state == "off_centre":
                return "  Centre your face  "
            elif gate_state in ("access_denied_unknown", "unknown"):
                return "  Unknown - Not registered  "
            elif gate_state == "multi_face":
                return "  One face only  "
            elif gate_state == "access_denied_expired":
                # Covers denied_expired and denied_no_payment (mapped same in gym_access.py)
                return f"  {name} - No active membership  " if name else "  No active membership  "
            elif gate_state == "access_denied_inactive":
                return f"  {name} - Inactive  " if name else "  Inactive member  "
            elif gate_state == "access_denied_emergency_lock":
                return "  Emergency lock active  "
            elif gate_state == "no_face":
                return ""
            else:
                return f"  {name}  " if name else "  -  "

        # ── Draw corner brackets instead of full rectangle ───────────────────
        def draw_corners(img, x1, y1, x2, y2, colour, thickness=2, length=20):
            # Top-left
            cv2.line(img, (x1, y1), (x1 + length, y1), colour, thickness)
            cv2.line(img, (x1, y1), (x1, y1 + length), colour, thickness)
            # Top-right
            cv2.line(img, (x2, y1), (x2 - length, y1), colour, thickness)
            cv2.line(img, (x2, y1), (x2, y1 + length), colour, thickness)
            # Bottom-left
            cv2.line(img, (x1, y2), (x1 + length, y2), colour, thickness)
            cv2.line(img, (x1, y2), (x1, y2 - length), colour, thickness)
            # Bottom-right
            cv2.line(img, (x2, y2), (x2 - length, y2), colour, thickness)
            cv2.line(img, (x2, y2), (x2, y2 - length), colour, thickness)

        # ── Draw filled label background + text ──────────────────────────────
        def draw_label(img, text, x, y, colour, font_scale=0.6, thickness=1):
            text = _cv_safe_text(text)
            font = cv2.FONT_HERSHEY_SIMPLEX
            (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
            pad = 4

            # Background pill
            bg_x1 = x
            bg_y1 = y - th - pad * 2
            bg_x2 = x + tw
            bg_y2 = y + baseline

            # Clamp to frame
            fh, fw = img.shape[:2]
            bg_x1 = max(0, bg_x1)
            bg_y1 = max(0, bg_y1)
            bg_x2 = min(fw, bg_x2)
            bg_y2 = min(fh, bg_y2)

            # Fill background with colour at 85% opacity blend
            roi = img[bg_y1:bg_y2, bg_x1:bg_x2]
            if roi.size > 0:
                bg = roi.copy()
                bg[:] = colour
                cv2.addWeighted(bg, 0.85, roi, 0.15, 0, roi)
                img[bg_y1:bg_y2, bg_x1:bg_x2] = roi

            # White text on coloured background
            text_y = max(bg_y1 + th + pad, th + pad)
            cv2.putText(img, text, (bg_x1, text_y),
                        font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)

        # ── Process each detection ────────────────────────────────────────────
        for d in detections:
            name       = d.get("class_name", "")
            conf       = d.get("confidence", 0.0)
            bbox       = d.get("bbox", [])
            gate_state = d.get("gate_state", "")

            colour = COLOURS.get(gate_state, DEFAULT_COLOUR)
            label  = get_label(gate_state, name, conf)

            if len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                fh, fw = annotated.shape[:2]

                # Clamp bbox to frame
                x1 = max(0, min(x1, fw - 1))
                y1 = max(0, min(y1, fh - 1))
                x2 = max(0, min(x2, fw - 1))
                y2 = max(0, min(y2, fh - 1))

                # Corner brackets — thicker for recognised/granted, normal for others
                bracket_thickness = 3 if gate_state in (
                    "recognised",
                    "already_present",
                    "cooldown",
                    "access_granted",
                    "access_granted_expiring",
                ) else 2
                bracket_length    = max(16, int((x2 - x1) * 0.15))
                draw_corners(annotated, x1, y1, x2, y2, colour,
                             thickness=bracket_thickness, length=bracket_length)

                # Thin full box outline at low opacity for context
                overlay = annotated.copy()
                cv2.rectangle(overlay, (x1, y1), (x2, y2), colour, 1)
                cv2.addWeighted(overlay, 0.25, annotated, 0.75, 0, annotated)

                # Label — positioned above box, clamped if near top
                font_scale = 0.65
                label_y = y1 - 6 if y1 > 30 else y2 + 22
                draw_label(annotated, label, x1, label_y, colour,
                           font_scale=font_scale, thickness=1)

            else:
                # No bbox — draw text centre-screen
                fh, fw = annotated.shape[:2]
                draw_label(annotated, label, fw // 2 - 80, 60, colour,
                           font_scale=0.8, thickness=1)

        return annotated