import { createRoot } from 'react-dom/client';
import { AuthController } from '../../mobile/src/auth/controller';
import { BrowserSessions } from './session';
import { browserGoogle } from './google';
import { createRuntime } from './runtime';
import { App } from './App';
import { restoreDevelopmentSession } from './development';
import './style.css';

const runtime = createRuntime(new BrowserSessions(window.sessionStorage), import.meta.env.VITE_API_BASE_URL ?? '');
const auth = new AuthController(runtime.api, browserGoogle(import.meta.env.VITE_GOOGLE_CLIENT_ID), runtime.sessions);
const developmentAccount = import.meta.env.DEV && import.meta.env.VITE_DEV_TEST_LOGIN === '1';
if (developmentAccount) await auth.restore();
await restoreDevelopmentSession(runtime.sessions, developmentAccount, window.location.hostname).catch(() => undefined);
createRoot(document.getElementById('root')!).render(<App runtime={runtime} auth={auth} developmentAccount={developmentAccount} />);
