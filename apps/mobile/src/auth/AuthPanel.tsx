import { useEffect, useRef, useState } from 'react';
import { Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import { AuthController, AuthState } from './controller';

declare const require: (id: string) => {
  GoogleSignInButton: React.ComponentType<{
    signInBehavior: 'none';
    onPress: () => void;
    size: 'wide';
    accessibilityLabel: string;
  }>;
};

export function AuthPanel({
  controller,
  onAccountChange,
}: {
  controller: AuthController;
  onAccountChange: () => void;
}) {
  const [state, setState] = useState<AuthState>(controller.getState());
  const accountChangeRef = useRef(onAccountChange);
  accountChangeRef.current = onAccountChange;

  useEffect(() => {
    let previousAccount: string | null | undefined;
    const unsubscribe = controller.subscribe((next) => {
      setState(next);
      const currentAccount = next.status === 'signed-in' ? next.account.id : null;
      if (previousAccount !== currentAccount) accountChangeRef.current();
      previousAccount = currentAccount;
    });
    void controller.restore();
    return unsubscribe;
  }, [controller]);

  if (state.status === 'signed-in') {
    return <View style={styles.container}>
      <Text accessibilityRole="text">Signed in</Text>
      {state.error ? <Text accessibilityRole="alert">{state.error}</Text> : null}
      <Pressable accessibilityRole="button" onPress={() => void controller.logout()} style={styles.button}>
        <Text>Sign out</Text>
      </Pressable>
    </View>;
  }

  if (state.status === 'signing-in') {
    return <View style={styles.container}>
      <Text accessibilityRole="text">
        {state.step === 'exchange' ? 'Finishing sign-in…' : 'Continue with Google to sign in.'}
      </Text>
      {state.step === 'challenge' ? <Pressable accessibilityRole="button"
        onPress={() => controller.cancelSignIn()} style={styles.button}>
        <Text>Cancel</Text>
      </Pressable> : null}
      {state.step === 'provider' ? <Text>Cancel in the Google sign-in window to stop.</Text> : null}
    </View>;
  }

  const googleConfigured = Boolean(process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID?.trim())
    && (Platform.OS !== 'ios' || Boolean(process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID?.trim()))
    && (Platform.OS === 'ios' || Platform.OS === 'android');
  const recoveryMessage = state.status === 'unavailable'
    ? state.error
    : state.status === 'signed-out' && state.error
      ? state.error
    : !googleConfigured
      ? 'Google sign-in is unavailable. Check setup or try again later.'
      : state.status === 'signed-out' ? 'Sign in to continue.' : 'Restoring session…';
  return <View style={styles.container}>
    <Text accessibilityRole={state.status === 'unavailable' || !googleConfigured ? 'alert' : 'text'}>
      {state.status === 'restoring' ? 'Restoring session…' : recoveryMessage}
    </Text>
    {state.status === 'unavailable' ? <>
      <Pressable accessibilityRole="button" onPress={() => void controller.restore()} style={styles.button}>
        <Text>Retry session</Text>
      </Pressable>
      <Pressable accessibilityRole="button" onPress={() => void controller.logout()} style={styles.button}>
        <Text>Retry sign out</Text>
      </Pressable>
    </> : null}
    {state.status !== 'restoring' && googleConfigured ? <GoogleSignInButton onPress={() => void controller.signIn()} /> : null}
  </View>;
}

function GoogleSignInButton({ onPress }: { onPress: () => void }) {
  if (!process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID?.trim()
    || (Platform.OS === 'ios' && !process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID?.trim())
    || (Platform.OS !== 'ios' && Platform.OS !== 'android')) return null;
  const { GoogleSignInButton: NativeButton } = require('react-native-nitro-google-signin');
  return <NativeButton
    signInBehavior="none"
    onPress={onPress}
    size="wide"
    accessibilityLabel="Continue with Google"
  />;
}

const styles = StyleSheet.create({
  container: { gap: 12, padding: 16 },
  button: { alignSelf: 'flex-start', paddingHorizontal: 16, paddingVertical: 10 },
});
