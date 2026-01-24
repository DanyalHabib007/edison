# Use a lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first (for better caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port your app uses (8080)
EXPOSE 8080

# Command to run the app
# "main:app" means file "main.py" and object "app"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
