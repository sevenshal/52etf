import logoDev from './logo_dev.svg';
import logoOnline from './logo_online.svg';

const appLogo = process.env.NODE_ENV === 'production' ? logoOnline : logoDev;

export default appLogo;
