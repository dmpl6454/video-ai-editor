/** @type {import('jest').Config} */
module.exports = {
  // jest-expo already sets the Expo-correct transformIgnorePatterns
  // (react-native, expo*, @expo*, @expo-google-fonts …); do not override them.
  preset: "jest-expo",
  setupFilesAfterEnv: ["<rootDir>/__tests__/helpers/jest.setup.ts"],
  testMatch: ["<rootDir>/__tests__/**/*.test.ts", "<rootDir>/__tests__/**/*.test.tsx"],
  testPathIgnorePatterns: ["/node_modules/", "/.expo/", "/dist/", "/__tests__/helpers/"],
  clearMocks: true,
  collectCoverageFrom: ["lib/**/*.ts", "constants/**/*.ts"],
};
