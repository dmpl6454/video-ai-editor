/**
 * jest-runtime's teardown walks every global to clear mocks. Expo's winter
 * runtime installs `fetch` as a lazy getter that loads expo-modules-core on
 * first access; by teardown, jest-expo's `globalThis.expo` mocks are gone, so
 * that first access produces a "Cannot log after tests are done" warning.
 * Resolve the getter now, while the mocks still exist.
 */
void globalThis.fetch;
