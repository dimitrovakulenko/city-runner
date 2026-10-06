import { useEffect, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { AuthController, AuthState } from './controller';

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

  const message = state.status === 'restoring'
    ? 'Restoring session…'
    : state.status === 'unavailable'
      ? 'Sign-in is unavailable. Check setup or try again later.'
      : state.error ?? 'Sign in to continue.';
  return <View style={styles.container}>
    <Text accessibilityRole={state.status === 'unavailable' ? 'alert' : 'text'}>{message}</Text>
    {state.status !== 'restoring' ? <Pressable accessibilityRole="button"
      onPress={() => void controller.signIn()} style={styles.button}>
      <Text>Continue with Google</Text>
    </Pressable> : null}
  </View>;
}

const styles = StyleSheet.create({
  container: { gap: 12, padding: 16 },
  button: { alignSelf: 'flex-start', paddingHorizontal: 16, paddingVertical: 10 },
});
