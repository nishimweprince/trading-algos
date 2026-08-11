'use client';

import { useEffect, useState } from 'react';
import {
  DEFAULT_STARRED,
  PROJECTS,
  STARRED_STORAGE_KEY,
  parseStarred,
  type Project,
} from '../../lib/projects';

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
      {filled ? (
        <path
          fill="currentColor"
          d="M12 2.5 14.9 9l7.1.6-5.4 4.6 1.7 6.8L12 17.8 5.7 21l1.7-6.8L2 9.6 9.1 9 12 2.5Z"
        />
      ) : (
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinejoin="round"
          d="M12 3.2 14.6 9l6.4.5-4.9 4.2 1.5 6.2L12 16.7 6.4 19.9l1.5-6.2L3 9.5 9.4 9 12 3.2Z"
        />
      )}
    </svg>
  );
}

function ProjectCard({
  project,
  starred,
  onToggle,
}: {
  project: Project;
  starred: boolean;
  onToggle: (id: string) => void;
}) {
  return (
    <article className={`ta-project-card${starred ? ' is-starred' : ''}`}>
      <div className="ta-project-card__header">
        <a href={project.href} className="ta-project-card__title">
          {project.title}
        </a>
        <button
          type="button"
          className="ta-star-btn"
          aria-pressed={starred}
          aria-label={starred ? `Unstar ${project.title}` : `Star ${project.title}`}
          title={starred ? 'Unstar project' : 'Star project'}
          onClick={(event) => {
            event.preventDefault();
            onToggle(project.id);
          }}
        >
          <StarIcon filled={starred} />
        </button>
      </div>
      <p className="ta-project-card__desc">{project.description}</p>
    </article>
  );
}

function ProjectSection({
  title,
  projects,
  starredIds,
  onToggle,
  empty,
}: {
  title: string;
  projects: Project[];
  starredIds: Set<string>;
  onToggle: (id: string) => void;
  empty?: string;
}) {
  return (
    <section className="ta-project-section">
      <h2 className="ta-project-section__title">{title}</h2>
      {projects.length === 0 ? (
        <p className="ta-project-section__empty">{empty}</p>
      ) : (
        <div className="ta-project-grid">
          {projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              starred={starredIds.has(project.id)}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </section>
  );
}

export function ProjectGrid() {
  const [starred, setStarred] = useState<string[]>(() => [...DEFAULT_STARRED]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const stored = parseStarred(window.localStorage.getItem(STARRED_STORAGE_KEY));
    if (stored) setStarred(stored);
    setReady(true);
  }, []);

  useEffect(() => {
    if (!ready) return;
    window.localStorage.setItem(STARRED_STORAGE_KEY, JSON.stringify(starred));
  }, [starred, ready]);

  const starredSet = new Set(starred);
  const starredProjects = starred
    .map((id) => PROJECTS.find((p) => p.id === id))
    .filter((p): p is Project => Boolean(p));
  const otherProjects = PROJECTS.filter((p) => !starredSet.has(p.id));

  function toggle(id: string) {
    setStarred((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  return (
    <div className="ta-projects" data-ready={ready ? 'true' : 'false'}>
      <p className="ta-projects__hint">
        Star projects to pin them here. Stars are saved in this browser.
      </p>
      <ProjectSection
        title="Starred"
        projects={starredProjects}
        starredIds={starredSet}
        onToggle={toggle}
        empty="No starred projects. Use the star on any card below."
      />
      <ProjectSection
        title="Catalog"
        projects={otherProjects}
        starredIds={starredSet}
        onToggle={toggle}
        empty="Every project is starred."
      />
    </div>
  );
}
