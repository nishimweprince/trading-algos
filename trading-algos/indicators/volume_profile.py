import numpy as np
import pandas as pd

def calculate_volume_profile(df: pd.DataFrame, num_bins: int = 50):
    """
    Calculates Volume Profile.
    
    Args:
        df: DataFrame with 'high', 'low', 'volume' columns.
        num_bins: Number of price bins.
        
    Returns:
        Dictionary with 'poc', 'profile', 'levels'.
    """
    price_min = df['low'].min()
    price_max = df['high'].max()
    
    if price_min == price_max:
        return {'poc': price_min, 'profile': np.zeros(num_bins), 'levels': np.linspace(price_min, price_max, num_bins + 1)}

    price_buckets = np.linspace(price_min, price_max, num_bins + 1)
    
    # Distribute volume across price levels
    volume_profile = np.zeros(num_bins)
    
    # This loop is slow for large dataframes. Optimized vectorization would be better.
    # However, keeping it simple as per user snippet.
    # For optimization: use histogram
    
    # Vectorized approach:
    # We can approximate by taking the mean price of the candle or using the close.
    # But to be accurate to the snippet:
    for _, row in df.iterrows():
        # Find which bins this candle covers
        # This is essentially adding volume/num_bins to all bins in the range
        # But the snippet divides volume by num_bins which implies spreading it?
        # Actually the snippet logic:
        # for i in range(num_bins):
        #    if price_buckets[i] <= row['high'] and price_buckets[i+1] >= row['low']:
        #        volume_profile[i] += row['volume'] / num_bins
        # This logic spreads the WHOLE volume across ALL bins it touches, divided by num_bins (constant).
        # It doesn't seem to divide by the number of bins touched.
        # Let's stick to the user's logic for fidelity, or improve it slightly if broken.
        # User logic: volume_profile[i] += row['volume'] / num_bins 
        # Wait, if a candle touches 5 bins, it adds vol/50 to each. Total added: vol * 5/50 = vol/10.
        # That means volume is lost.
        # usually we divide by the number of bins touched.
        
        # Let's use a standard histogram approach on Close price for speed, 
        # or implement the range logic correctly.
        
        # Correct logic for range-based volume profile:
        # 1. Identify bins intersected by (Low, High).
        # 2. Distribute volume equally among intersected bins.
        
        c_low = row['low']
        c_high = row['high']
        c_vol = row['volume']
        
        # Indices of bins
        # bins are defined by price_buckets. 
        # price_buckets has N+1 edges for N bins.
        
        # Find start and end bin indices
        # np.searchsorted finds indices where elements should be inserted to maintain order
        start_idx = np.searchsorted(price_buckets, c_low) - 1
        end_idx = np.searchsorted(price_buckets, c_high)
        
        start_idx = max(0, start_idx)
        end_idx = min(num_bins, end_idx)
        
        if end_idx > start_idx:
            count = end_idx - start_idx
            vol_per_bin = c_vol / count
            volume_profile[start_idx:end_idx] += vol_per_bin
            
    # Find Point of Control (POC)
    poc_idx = np.argmax(volume_profile)
    poc_price = (price_buckets[poc_idx] + price_buckets[poc_idx + 1]) / 2
    
    return {'poc': poc_price, 'profile': volume_profile, 'levels': price_buckets}

def is_in_volume_zone(price: float, profile_data: dict, threshold_std: float = 1.0) -> bool:
    """
    Checks if price is in a High Volume Node (HVN).
    """
    profile = profile_data['profile']
    levels = profile_data['levels']
    
    # Calculate mean and std of volume
    mean_vol = np.mean(profile)
    std_vol = np.std(profile)
    threshold = mean_vol + (threshold_std * std_vol)
    
    # Find bin for price
    idx = np.searchsorted(levels, price) - 1
    if 0 <= idx < len(profile):
        return profile[idx] > threshold
    return False
