# Use a lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# --- FIX START: Install Timezone Data ---
# 1. Install tzdata package (required for slim images)
# 2. Clean up apt list to keep image small
RUN apt-get update && \
    apt-get install -y tzdata && \
    rm -rf /var/lib/apt/lists/*

# 3. Set the Timezone Environment Variable
ENV TZ=Asia/Kolkata
# --- FIX END ---

# Copy the requirements file first (for better caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port your app uses (8080)
EXPOSE 8080

# Command to run the app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
