FROM node:20-alpine

WORKDIR /app

# Install dependencies first for caching
COPY package.json package-lock.json* ./
RUN npm install

# Copy source code
COPY . .

# Run development server
CMD ["npm", "run", "dev"]
