import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import CampaignLive from './CampaignLive';

createRoot(document.getElementById('root')!).render(<StrictMode><CampaignLive /></StrictMode>);
