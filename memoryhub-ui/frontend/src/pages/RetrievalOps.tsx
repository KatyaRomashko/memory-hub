import React from 'react';
import { Hub, useApp } from '@/components/Shell';
import { type HashRoute } from '@/hooks/useHashRoute';
import Operations from './Operations';
import Quality from './Quality';

/** Admin menu item "Retrieval health & operations": search health and the tenant's background jobs. */
const RetrievalOps: React.FC<{ route: HashRoute }> = ({ route }) => {
  const { me } = useApp();
  const labs = Boolean(me?.labs);
  return (
    <Hub
      title="Retrieval health & operations"
      description="How search behaves for every agent in the tenant — latency, what gets cut and why — and the background jobs that keep memory indexed, curated and compacted."
      route={route}
      fallback={labs ? 'health' : 'jobs'}
      views={[
        ...(labs ? [{ value: 'health' as const, label: 'Retrieval health', render: () => <Quality route={route} /> }] : []),
        { value: 'jobs' as const, label: 'Background jobs', render: () => <Operations /> },
      ]}
    />
  );
};

export default RetrievalOps;
