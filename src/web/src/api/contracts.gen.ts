/* Generated from contracts/schemas/forecast.schema.json — do not edit. */

/**
 * Public aggregate forecast for a future interval. Suppressed values are null, never numeric zero.
 */
export type OperationalAggregateForecast = {
  [k: string]: unknown;
} & {
  schema_version: "1.0.0";
  tenant_id: string;
  forecast_id: string;
  cell_id: string;
  window_start: string;
  window_end: string;
  category: string;
  generated_at: string;
  data_as_of: string;
  expected_count: Estimate;
  occurrence_probability: ProbabilityEstimate;
  risk_band: "low" | "typical" | "elevated" | "high" | "suppressed";
  coverage_ratio: number;
  /**
   * @maxItems 5
   */
  drivers:
    | []
    | [
        {
          feature: string;
          direction: "higher" | "lower";
        }
      ]
    | [
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        }
      ]
    | [
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        }
      ]
    | [
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        }
      ]
    | [
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        },
        {
          feature: string;
          direction: "higher" | "lower";
        }
      ];
  model_version: string;
  data_version: string;
  feature_snapshot_version: string;
  suppression: {
    suppressed: boolean;
    reason: null | "low_support" | "low_coverage" | "policy";
  };
};

export interface Estimate {
  value: number | null;
  lower: number | null;
  upper: number | null;
  interval_level: number;
  method: string;
}
export interface ProbabilityEstimate {
  value: number | null;
  lower: number | null;
  upper: number | null;
  interval_level: number;
  method: string;
  calibration_version: string | null;
}
/* Generated from contracts/schemas/candidate-detection.schema.json — do not edit. */

/**
 * A machine-generated candidate safety incident requiring human review. It is not a confirmed crime or forecast.
 */
export interface RestrictedCandidateDetection {
  schema_version: "1.1.0";
  tenant_id: string;
  detection_id: string;
  source_id: string;
  asset_id?: string;
  occurred_at: string;
  received_at: string;
  proposed_category: "property" | "violence" | "public_order" | "traffic_safety" | "other" | "unmapped";
  event_type: string;
  description: string;
  confidence: number;
  detector_version: string;
  evidence_ref: string;
  review_status: "awaiting_review" | "confirmed" | "rejected" | "expired";
  expires_at: string;
}
/* Generated from contracts/schemas/candidate-review.schema.json — do not edit. */

/**
 * Immutable, tenant-scoped human review decision. Only confirmed decisions may promote an IncidentEvent.
 */
export type CandidateReviewDecision = {
  [k: string]: unknown;
} & {
  schema_version: "1.0.0";
  tenant_id: string;
  review_id: string;
  detection_id: string;
  decision: "confirmed" | "rejected";
  confirmed_category?: "property" | "violence" | "public_order" | "traffic_safety" | "other";
  rejection_reason?: "false_positive" | "insufficient_evidence" | "duplicate" | "outside_scope" | "other";
  reviewed_by: string;
  reviewed_at: string;
  promoted_external_event_id?: string;
};
/* Generated from contracts/schemas/coverage-snapshot.schema.json — do not edit. */

/**
 * Per-source availability telemetry for one UTC feature interval.
 */
export interface SourceCoverageSnapshot {
  schema_version: "1.0.0";
  tenant_id: string;
  source_id: string;
  interval_start: string;
  interval_end: string;
  expected_seconds: number;
  connected_seconds: number;
  processable_seconds: number;
  detector_available_seconds: number;
  coverage_ratio: number;
  /**
   * @maxItems 20
   */
  degraded_reason_codes:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
  computed_at: string;
}
/* Generated from contracts/schemas/model-card.schema.json — do not edit. */

export interface AggregateForecastModelCard {
  schema_version: "1.0.0";
  tenant_id: string;
  model_version: string;
  data_version: string;
  generated_at: string;
  model_name: "historical_rate" | "previous_period" | "regularized_poisson" | "lightgbm_poisson" | "lightgbm_tweedie";
  target: "next_window_count" | "next_window_occurrence";
  prediction_unit: string;
  training_period: Period;
  evaluation_period: Period;
  primary_metric: CardMetric;
  baseline_comparison: {
    baseline_model: "historical_rate" | "previous_period";
    baseline_value: number;
    selected_value: number;
    relative_gain: number;
    selected_model_beats_baseline: boolean;
  };
  /**
   * @minItems 1
   * @maxItems 10
   */
  intended_uses:
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string];
  /**
   * @minItems 1
   * @maxItems 20
   */
  prohibited_uses:
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
  /**
   * @minItems 1
   * @maxItems 20
   */
  limitations:
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
  uncertainty_method: string;
  suppression_policy: string;
  feature_interpretation: string;
  human_review_required: true;
}
export interface Period {
  start: string;
  end: string;
}
export interface CardMetric {
  name: "poisson_deviance" | "mae" | "pr_auc" | "brier_score";
  value: number;
  split: "test";
  definition: string;
}
/* Generated from contracts/schemas/camera-source.schema.json — do not edit. */

/**
 * Tenant-owned recorded-video or live-camera source. Credentials and exact location are referenced, never embedded.
 */
export type CameraSource = {
  [k: string]: unknown;
} & {
  schema_version: "1.0.0";
  tenant_id: string;
  source_id: string;
  name: string;
  mode: "recorded_video" | "live_camera";
  status: "draft" | "validating" | "active" | "degraded" | "paused" | "archived";
  timezone: string;
  location_ref: string;
  connection: {
    transport: "uploaded_asset" | "hls" | "rtsp" | "onvif";
    endpoint_ref?: string;
    credential_ref?: string;
  };
  retention_policy_days: number;
  created_at: string;
  updated_at?: string;
};
/* Generated from contracts/schemas/api-error.schema.json — do not edit. */

export interface TypedApiError {
  schema_version: "1.0.0";
  request_id: string;
  code: string;
  message: string;
  retryable: boolean;
  /**
   * @maxItems 20
   */
  details:
    | []
    | [
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ]
    | [
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        },
        {
          field: string;
          code: string;
        }
      ];
}
