-- Backfill the additive candidate 1.1 observation fields. Legacy candidates
-- remain explicitly unclassified; the migration must not invent an event from
-- old model output. New candidates are produced and validated by the runtime.
BEGIN;

CREATE POLICY candidate_observation_migration
  ON public.candidate_detections_restricted
  TO PUBLIC
  USING (current_user = 'crime_migrator')
  WITH CHECK (current_user = 'crime_migrator');

UPDATE public.candidate_detections_restricted
SET candidate = candidate || jsonb_build_object(
  'schema_version', '1.1.0',
  'event_type', CASE
    WHEN candidate ? 'event_type'
      AND candidate->>'event_type' IN (
        'property_damage', 'vandalism', 'forced_entry', 'physical_fight',
        'weapon_assault', 'crowd_crush', 'crowd_disturbance',
        'vehicle_collision', 'vehicle_pedestrian_collision', 'road_obstruction',
        'structural_collapse', 'falling_debris', 'fire', 'explosion',
        'medical_emergency', 'person_fall', 'other_acute_hazard',
        'legacy_unclassified'
      )
    THEN candidate->>'event_type'
    ELSE 'legacy_unclassified'
  END,
  'description', CASE
    WHEN candidate ? 'description'
      AND char_length(btrim(candidate->>'description')) BETWEEN 2 AND 240
      AND right(btrim(candidate->>'description'), 1) IN ('.', '!', '?')
      AND position(E'\n' IN candidate->>'description') = 0
      AND position(E'\r' IN candidate->>'description') = 0
    THEN btrim(candidate->>'description')
    ELSE 'No structured Reka description is available for this legacy candidate; review the video evidence.'
  END
)
WHERE candidate->>'schema_version' IS DISTINCT FROM '1.1.0'
   OR NOT candidate ? 'event_type'
   OR NOT candidate ? 'description';

DROP POLICY candidate_observation_migration
  ON public.candidate_detections_restricted;

COMMIT;
