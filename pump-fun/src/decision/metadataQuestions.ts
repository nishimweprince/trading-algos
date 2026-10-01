import type { Question } from './types.ts';

/**
 * The metadata battery (shadow only): text judgments the heuristic soft score
 * makes crudely today (+10 for any social link, +5 for a name/symbol). Each is
 * atomic and recorded against outcomes by `research:decision metadata-report`
 * before anything may gate on it. Bump the version on ANY prompt change.
 */
export const METADATA_QUESTION_SET_VERSION = 1;

export const METADATA_QUESTION_IDS = ['impersonation', 'low_effort', 'socials_credible', 'narrative'] as const;

export function buildMetadataQuestions(): Question[] {
  return [
    {
      id: 'impersonation',
      kind: 'probability',
      prompt:
        'This is a newly launched pump.fun token. Does its name or symbol impersonate or ride on a well-known brand, ' +
        'person, existing cryptocurrency, or trending news story?',
    },
    {
      id: 'low_effort',
      kind: 'probability',
      prompt:
        'Does this token metadata look low-effort or auto-generated: a random string, placeholder, or name with no ' +
        'recognisable concept?',
    },
    {
      id: 'socials_credible',
      kind: 'probability',
      prompt:
        'Do the social links look like a real, dedicated presence for this specific project, rather than missing, ' +
        'generic, or borrowed from someone else?',
    },
    {
      id: 'narrative',
      kind: 'choice',
      prompt: "Which best describes this token's narrative?",
      options: ['celebrity_or_politics', 'meme_animal', 'ai_or_tech', 'news_event', 'copy_of_existing_token', 'random_or_none', 'other'],
    },
  ];
}
