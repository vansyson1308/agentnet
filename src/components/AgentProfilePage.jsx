import React from 'react';
import { useParams } from 'react-router-dom';
import MissionPanel from './MissionPanel';

function AgentProfilePage() {
  const { id } = useParams();

  return (
    <div className="agent-profile">
      <h2>Agent Profile</h2>
      <MissionPanel agentId={id} />
      {/* Other profile sections unchanged */}
    </div>
  );
}

export default AgentProfilePage;