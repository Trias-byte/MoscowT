import reference from './reference-network.json';
import { NetworkSchema } from '../lib/contracts';
export const network = NetworkSchema.parse(reference);
