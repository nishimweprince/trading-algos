import { existsSync, readFileSync } from 'node:fs';
import { logger } from '../core/logger.ts';
import { contributions, predictProba, type LogRegModel } from '../research/logreg.ts';
import { FEATURE_NAMES, toFeatureVector, type FeatureInput } from '../research/featureSpec.ts';

/**
 * Learned filter inference (work plan 2026-09-25 P3.4). Loads the JSON a
 * `npm run research:train` run wrote and scores every screened candidate.
 *
 * Scoring always happens when a model file is present — model_version and
 * model_prob are recorded on every candidate, so the model is evaluated in
 * shadow before it is ever allowed to gate. `model.enabled` only decides
 * whether a low probability vetoes (MODEL_SKIP) and, with sizeByProb, scales
 * size.
 */
export interface MetaModelFile extends LogRegModel {
  version: string;
  createdAt: string;
  threshold: number;
}

export interface ModelScore {
  version: string;
  prob: number;
  threshold: number;
  take: boolean;
  top: Array<{ feature: string; logit: number }>;
}

export class MetaModel {
  readonly file: MetaModelFile;

  constructor(file: MetaModelFile) {
    this.file = file;
  }

  static load(path: string): MetaModel | null {
    const log = logger.child({ mod: 'model' });
    if (!existsSync(path)) return null;
    try {
      const file = JSON.parse(readFileSync(path, 'utf8')) as MetaModelFile;
      const expected = FEATURE_NAMES.join(',');
      if (file.featureNames.join(',') !== expected) {
        log.error('model feature spec does not match this build — model ignored', {
          path,
          modelFeatures: file.featureNames.length,
          buildFeatures: FEATURE_NAMES.length,
        });
        return null;
      }
      log.info('meta model loaded', { path, version: file.version, threshold: file.threshold });
      return new MetaModel(file);
    } catch (err) {
      log.error('meta model load failed — model ignored', { path, err });
      return null;
    }
  }

  score(input: FeatureInput, minProb?: number): ModelScore {
    const x = toFeatureVector(input);
    const prob = predictProba(this.file, x);
    const threshold = minProb ?? this.file.threshold;
    return { version: this.file.version, prob, threshold, take: prob >= threshold, top: contributions(this.file, x) };
  }
}

/** Meta-label bet sizing: 1 at the threshold, up to 1.25 when confident, never below 0.5. */
export function sizeFactorForProb(prob: number, threshold: number): number {
  if (!(threshold > 0)) return 1;
  return Math.min(1.25, Math.max(0.5, prob / threshold));
}
