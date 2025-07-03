import axios from 'axios';

const cnnRequest = axios.create({
  baseURL: 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata',
  timeout: 10000,
  headers: {
    'Accept': 'application/json',
    'Origin': window.location.origin
  }
});

export const fetchFearGreedData = async (days = 0) => {
  try {
    var dateStr = '2021-01-22';
    if (days >= 0) {
      const today = new Date();
      const startDate = new Date(today);
      startDate.setDate(today.getDate() - days);
      dateStr = startDate.toISOString().split('T')[0];
    }
    
    const response = await cnnRequest.get(`/${dateStr}`);
    return response.data;
  } catch (error) {
    console.error('Failed to fetch CNN Fear & Greed data:', error);
    throw error;
  }
}; 