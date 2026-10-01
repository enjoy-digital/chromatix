//
// Copyright(C) 1993-1996 Id Software, Inc.
// Copyright(C) 2005-2014 Simon Howard
//
// This program is free software; you can redistribute it and/or
// modify it under the terms of the GNU General Public License
// as published by the Free Software Foundation; either version 2
// of the License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// DESCRIPTION:
//	Fixed point implementation.
//



#include "stdlib.h"

#include "doomtype.h"
#include "i_system.h"

#include "m_fixed.h"




// Fixme. __USE_C_FIXED__ or something.

fixed_t
FixedMul
( fixed_t	a,
  fixed_t	b )
{
    return ((int64_t) a * (int64_t) b) >> FRACBITS;
}



//
// FixedDiv, C version.
//

fixed_t FixedDiv(fixed_t a, fixed_t b)
{
    if ((abs(a) >> 14) >= abs(b))
    {
	return (a^b) < 0 ? INT_MIN : INT_MAX;
    }
    else
    {
#ifdef DOOMGENERIC_FIXEDDIV32
	// 32-bit only (no 64-bit software division), bit exact with the 64-bit version:
	// |a|*65536/|b| = (|a|/|b|)*65536 + ((|a|%|b|)*65536)/|b|, truncated toward zero.
	uint32_t ua = a < 0 ? -(uint32_t) a : (uint32_t) a;
	uint32_t ub = b < 0 ? -(uint32_t) b : (uint32_t) b;
	uint32_t q  = ua / ub;
	uint32_t r  = ua - q * ub;
	uint32_t f, m;
	int      i;

	if (ub < 0x10000)
	{
	    f = (r << 16) / ub;
	}
	else
	{
	    f = 0;
	    for (i = 0; i < 16; i++)
	    {
		uint32_t carry = r >> 31;
		r <<= 1;
		f <<= 1;
		if (carry || r >= ub)
		{
		    r -= ub;
		    f |= 1;
		}
	    }
	}
	m = (q << 16) + f;
	return (a^b) < 0 ? -(int32_t) m : (int32_t) m;
#else
	int64_t result;

	result = ((int64_t) a << 16) / b;

	return (fixed_t) result;
#endif
    }
}

